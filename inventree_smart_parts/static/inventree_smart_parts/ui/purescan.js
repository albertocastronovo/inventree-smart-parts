/**
 * PureScan – Zero-Click Barcode State Machine  (v2 – Combo/Undo/Rich)
 * =====================================================================
 * A 100% mouse-free warehouse terminal.  Operators execute InvenTree
 * actions purely by sequential barcode scans.
 *
 * v2 additions:
 *   - Rich Info mode (part image, location path, tracking history)
 *   - Combo-timer quantity accumulation for Add/Remove/Stocktake
 *   - Action log with Undo stack
 */
(function (global) {
  'use strict';

  // ═══════════════════════════════════════════════════════════════════════════
  //  Constants
  // ═══════════════════════════════════════════════════════════════════════════

  const STATES = {
    IDLE:              'IDLE',
    AWAITING_ITEM:     'AWAITING_ITEM',
    AWAITING_PARAM:    'AWAITING_PARAM',
    AWAITING_LOCATION: 'AWAITING_LOCATION',  // v3: SYS:LAST / CREATESTOCK location step
    AWAITING_QTY:      'AWAITING_QTY',       // v3: CREATESTOCK quantity step
    AWAITING_TOKEN:    'AWAITING_TOKEN',     // v3: CREATESTOCK token binding step
    AWAITING_CONFIRM:  'AWAITING_CONFIRM',   // v3: DELETEPART/DELETESTOCK confirm
    COMBO:             'COMBO',              // accumulating qty scans
    PROCESSING:        'PROCESSING',
    SUCCESS:           'SUCCESS',
    ERROR:             'ERROR',
  };

  const ACTIONS = {
    TRANSFER:    'TRANSFER',
    INFO:        'INFO',
    ADD:         'ADD',
    REMOVE:      'REMOVE',
    STOCKTAKE:   'STOCKTAKE',
    CREATEPART:  'CREATEPART',
    CREATESTOCK: 'CREATESTOCK',
    FASTADD:     'FASTADD',
    DELETEPART:  'DELETEPART',
    DELETESTOCK: 'DELETESTOCK',
    LAST:        'LAST',
  };

  // Map control-code strings to actions
  const CONTROL_CODES = {
    'SYS:TRANSFER':    ACTIONS.TRANSFER,
    'SYS:INFO':        ACTIONS.INFO,
    'SYS:ADD':         ACTIONS.ADD,
    'SYS:REMOVE':      ACTIONS.REMOVE,
    'SYS:STOCKTAKE':   ACTIONS.STOCKTAKE,
    'SYS:CREATEPART':  ACTIONS.CREATEPART,
    'SYS:CREATESTOCK': ACTIONS.CREATESTOCK,
    'SYS:FASTADD':     ACTIONS.FASTADD,
    'SYS:DELETEPART':  ACTIONS.DELETEPART,
    'SYS:DELETESTOCK': ACTIONS.DELETESTOCK,
    'SYS:LAST':        ACTIONS.LAST,
    // Aliases
    'SYS:MOVE':        ACTIONS.TRANSFER,
    'SYS:LOOKUP':      ACTIONS.INFO,
  };

  const RESULT_DISPLAY_MS = 4000;
  const COMBO_TIMEOUT_MS  = 3000;   // Time to wait for additional qty scans
  const BURST_TIMEOUT_MS  = 30000;
  const MAX_UNDO_HISTORY  = 20;
  const LOCALSTORAGE_KEY  = 'purescan_history';
  const SYS_LAST_KEY      = 'purescan_last_location';
  const TOKEN_RE          = /^(STOCK|LOC)_[A-Z0-9]{4}$/;

  // ═══════════════════════════════════════════════════════════════════════════
  //  State
  // ═══════════════════════════════════════════════════════════════════════════

  let _state        = STATES.IDLE;
  let _action       = null;
  let _stockItemId  = null;
  let _itemInfo     = null;
  let _resultTimer  = null;
  let _onStateChange = null;

  // Combo-timer state
  let _comboQty     = 0;
  let _comboTimer   = null;

  // v1.2.6: FASTADD burst window
  let _burstTimer   = null;
  let _burstDeadline = 0;

  // Action history / undo stack
  let _actionLog  = [];    // { ts, description, payload, actionType, undone }

  // CREATESTOCK pending state (v3)
  let _pendingPartId      = null;
  let _pendingPartName    = '';
  let _pendingCategoryNote = '';    // v1.2.3: category path shown in prompts
  let _pendingPricingNote = '';
  let _flowId = 0;
  let _pendingLocationId  = null;
  let _pendingQty         = null;
  let _pendingBatch       = '';
  let _pendingBarcodeData = null;   // parsed distributor label fields

  // DELETE pending state (v3)
  let _pendingDeleteKind  = null;   // 'part' | 'stock'
  let _pendingDeleteId    = null;
  let _pendingDeleteDesc  = '';
  let _pendingDeleteRaw   = null;   // first-scan raw payload for re-scan compare
  let _pendingToken       = null;

  // ═══════════════════════════════════════════════════════════════════════════
  //  Barcode parsing helpers
  // ═══════════════════════════════════════════════════════════════════════════

  async function _parseInvenTreeBarcode(raw) {
    const trimmed = raw.trim();

    // Format 0: SmartParts custom tokens  STOCK_XXXX / LOC_XXXX
    if (TOKEN_RE.test(trimmed)) {
      try {
        const resp = await fetch('/plugin/smartparts/api/purescan/token/', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-CSRFToken': _getCSRF(),
          },
          body: JSON.stringify({ token: trimmed }),
        });
        if (resp.ok) {
          const data = await resp.json();
          if (data.type === 'free') {
            return { type: 'free', token: trimmed };
          }
          if (data.type && data.id) {
            return {
              type: data.type,
              id: parseInt(data.id, 10),
              name: data.name || '',
              partId: data.part_id || null,
              locationId: data.location_id || null,
              token: trimmed,
            };
          }
        }
      } catch (e) {
        console.warn('[PureScan] Token lookup failed:', e);
      }
      return { type: 'unresolved_token', token: trimmed };
    }

    // Format 1: JSON  {"stockitem": 722}
    if (trimmed.startsWith('{') && trimmed.endsWith('}')) {
      try {
        const obj = JSON.parse(trimmed);
        for (const key of ['stockitem', 'stocklocation', 'part']) {
          if (obj[key] != null) {
            return { type: key, id: parseInt(obj[key], 10) };
          }
        }
      } catch (e) { /* not JSON */ }
    }

    // Format 2: INvenTree shortcodes  INV-SI<n> / INV-SL<n> / INV-PA<n>
    const shortcodeMap = { SI: 'stockitem', SL: 'stocklocation', PA: 'part' };
    const scMatch = trimmed.match(/^INV-([A-Z]{2})(\d+)$/i);
    if (scMatch) {
      const type = shortcodeMap[scMatch[1].toUpperCase()];
      if (type) return { type, id: parseInt(scMatch[2], 10) };
    }

    // Format 3: key=value / key:value
    const kvMatch = trimmed.match(/^(stockitem|stocklocation|part)[=:](\d+)$/i);
    if (kvMatch) {
      return { type: kvMatch[1].toLowerCase(), id: parseInt(kvMatch[2], 10) };
    }

    // Format 4: Fallback to backend API for linked barcodes
    try {
      const resp = await fetch(
        `/plugin/smartparts/api/purescan/resolve/?barcode=${encodeURIComponent(trimmed)}`,
        { headers: { Accept: 'application/json' } }
      );
      if (resp.ok) {
        const data = await resp.json();
        if (data.type && data.id) {
          return { type: data.type, id: parseInt(data.id, 10), name: data.name || '' };
        }
      }
    } catch (e) {
      console.warn('[PureScan] Backend barcode lookup failed:', e);
    }

    return { type: null, id: null };
  }

  function _parseControlCode(raw) {
    const upper = raw.trim().toUpperCase();
    if (upper === 'SYS:UNDO') return '__UNDO__';
    return CONTROL_CODES[upper] || null;
  }

  function _parseQuantityCode(raw) {
    const m = raw.trim().toUpperCase().match(/^SYS:QTY:(\d+(?:\.\d+)?)$/);
    return m ? parseFloat(m[1]) : null;
  }

  // Action-aware prompt: what to scan next after entering AWAITING_ITEM
  function _itemPrompt(action) {
    switch (action) {
      case ACTIONS.CREATEPART:  return 'Now scan a distributor DataMatrix label';
      case ACTIONS.CREATESTOCK: return 'Now scan a distributor label, an existing STOCK_ token, or a part label';
      case ACTIONS.FASTADD:     return 'Now scan a distributor DataMatrix or an instance (STOCK_) label';
      case ACTIONS.DELETEPART:  return 'Now scan a distributor DataMatrix label';
      case ACTIONS.DELETESTOCK: return 'Now scan a STOCK_XXXX token bound to a stock item';
      case ACTIONS.LAST:        return 'Now scan a Location label';
      default:                  return 'Now scan a Stock Item label';
    }
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  State transitions
  // ═══════════════════════════════════════════════════════════════════════════

  function _setState(newState, detail, extra) {
    _state = newState;
    if (_onStateChange) {
      _onStateChange({
        state:       _state,
        action:      _action,
        stockItemId: _stockItemId,
        itemInfo:    _itemInfo,
        detail:      detail || '',
        comboQty:    _comboQty,
        actionLog:   _actionLog,
        ...extra,
      });
    }
  }

  function _resetToIdle(detail) {
    _flowId++;
    _action      = null;
    _stockItemId = null;
    _itemInfo    = null;
    _comboQty    = 0;
    _clearPendingCreate();
    _clearPendingDelete();
    clearTimeout(_resultTimer);
    clearTimeout(_comboTimer);
    _stopBurst();
    _setState(STATES.IDLE, detail || 'Ready — Scan an Action Code');
  }

  function _showResult(state, detail, autoResetMs) {
    _setState(state, detail);
    clearTimeout(_resultTimer);
    _resultTimer = setTimeout(() => _resetToIdle(), autoResetMs || RESULT_DISPLAY_MS);
  }

  function _stopBurst() {
    clearTimeout(_burstTimer);
    _burstTimer = null;
    _burstDeadline = 0;
  }

  // Keep the terminal armed for the next reel after each FASTADD commit.
  function _startBurst(successMsg) {
    _stopBurst();
    clearTimeout(_resultTimer);
    _burstDeadline = Date.now() + BURST_TIMEOUT_MS;
    _setState(STATES.SUCCESS, `${successMsg}\n⚡ Burst: scan next DataMatrix within ${BURST_TIMEOUT_MS / 1000}s`);
    _burstTimer = setTimeout(() => _resetToIdle('Burst ended — scan an Action Code'), BURST_TIMEOUT_MS);
  }

  function _armAction(action) {
    _flowId++;
    clearTimeout(_resultTimer);
    clearTimeout(_comboTimer);
    _clearPendingCreate();
    _clearPendingDelete();
    _comboQty = 0;
    _action = action;
    _stockItemId = null;
    _itemInfo = null;
    _setState(STATES.AWAITING_ITEM, `Action: ${action} — ${_itemPrompt(action)}`);
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  Action Log / Undo  (persisted to localStorage)
  // ═══════════════════════════════════════════════════════════════════════════

  function _saveLog() {
    try {
      window.localStorage.setItem(LOCALSTORAGE_KEY, JSON.stringify(_actionLog));
    } catch (e) { /* quota exceeded or private mode — ignore */ }
  }

  function _loadLog() {
    try {
      const raw = window.localStorage.getItem(LOCALSTORAGE_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (Array.isArray(parsed)) {
          _actionLog = parsed.slice(0, MAX_UNDO_HISTORY);
        }
      }
    } catch (e) { /* corrupt data — ignore */ }
  }

  function clearLog() {
    _actionLog.length = 0;
    _saveLog();
    // Re-render by emitting current state
    _setState(_state, 'History cleared');
  }

  function _pushLog(entry) {
    _actionLog.unshift(entry);
    if (_actionLog.length > MAX_UNDO_HISTORY) _actionLog.pop();
    _saveLog();
  }

  // Only quantity-affecting operations are undoable; create/delete entries
  // stay in the history display but are skipped by SYS:UNDO.
  const UNDOABLE_TYPES = new Set(['ADD', 'REMOVE', 'TRANSFER', 'STOCKTAKE']);

  function _getLastUndoable() {
    return _actionLog.find(e => !e.undone && UNDOABLE_TYPES.has(e.actionType));
  }

  // ── SYS:LAST default location (per terminal, localStorage) ──
  function _getLastLocation() {
    try {
      const raw = window.localStorage.getItem(SYS_LAST_KEY);
      if (raw) {
        const parsed = JSON.parse(raw);
        if (parsed && parsed.id) return parsed;
      }
    } catch (e) { /* corrupt data — ignore */ }
    return null;
  }

  function _setLastLocation(loc) {
    try {
      window.localStorage.setItem(SYS_LAST_KEY, JSON.stringify(loc));
    } catch (e) { /* quota exceeded — ignore */ }
  }

  // ── Pending-state clear helpers ──
  function _clearPendingCreate() {
    _pendingPartId      = null;
    _pendingPartName    = '';
    _pendingCategoryNote = '';
    _pendingPricingNote = '';
    _pendingLocationId  = null;
    _pendingQty         = null;
    _pendingBatch       = '';
    _pendingBarcodeData = null;
  }

  function _clearPendingDelete() {
    _pendingDeleteKind = null;
    _pendingDeleteId   = null;
    _pendingDeleteDesc = '';
    _pendingDeleteRaw  = null;
    _pendingToken      = null;
  }

  async function _executeUndo() {
    const last = _getLastUndoable();
    if (!last) {
      _showResult(STATES.ERROR, 'Nothing to undo', 2000);
      return;
    }

    _setState(STATES.PROCESSING, `Undoing: ${last.description}`);
    try {
      const p = last.payload;
      let undoResult = '';

      if (last.actionType === 'ADD') {
        await _apiPost('/api/stock/remove/', {
          items: [{ pk: p.stockItemId, quantity: p.quantity }],
          notes: `PureScan UNDO: Reverting Add of ${p.quantity}`,
        });
        undoResult = `↩ Removed ${p.quantity}× "${p.partName}" (reverted Add)`;

      } else if (last.actionType === 'REMOVE') {
        await _apiPost('/api/stock/add/', {
          items: [{ pk: p.stockItemId, quantity: p.quantity }],
          notes: `PureScan UNDO: Reverting Remove of ${p.quantity}`,
        });
        undoResult = `↩ Added back ${p.quantity}× "${p.partName}" (reverted Remove)`;

      } else if (last.actionType === 'TRANSFER') {
        await _apiPost('/api/stock/transfer/', {
          items: [{ pk: p.stockItemId, quantity: p.quantity }],
          location: p.fromLocationId,
          notes: `PureScan UNDO: Reverting transfer back to ${p.fromLocation}`,
        });
        undoResult = `↩ Moved "${p.partName}" back to ${p.fromLocation} (reverted Transfer)`;

      } else if (last.actionType === 'STOCKTAKE') {
        await _apiPost('/api/stock/count/', {
          items: [{ pk: p.stockItemId, quantity: p.previousQty }],
          notes: `PureScan UNDO: Reverting Stocktake (was ${p.quantity}, restoring ${p.previousQty})`,
        });
        undoResult = `↩ Restored qty to ${p.previousQty} for "${p.partName}" (reverted Stocktake)`;

      } else {
        _showResult(STATES.ERROR, `Cannot undo action type: ${last.actionType}`, 3000);
        return;
      }

      last.undone = true;
      _pushLog({
        ts: new Date(),
        description: undoResult,
        actionType: 'UNDO',
        undone: true,
        payload: null,
      });
      _saveLog();   // Persist the undone flag change
      _showResult(STATES.SUCCESS, undoResult, 5000);

    } catch (e) {
      _showResult(STATES.ERROR, `Undo failed: ${e.message}`, 5000);
    }
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  API helpers
  // ═══════════════════════════════════════════════════════════════════════════

  function _getCSRF() {
    const el = document.getElementById('ps-csrf');
    if (el) return el.value;
    const match = document.cookie.match(/csrftoken=([^;]+)/);
    return match ? match[1] : '';
  }

  async function _apiPost(url, body) {
    const resp = await fetch(url, {
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'X-CSRFToken': _getCSRF(),
      },
      body: JSON.stringify(body),
    });
    if (!resp.ok) {
      const errData = await resp.json().catch(() => ({}));
      throw new Error(errData.detail || errData.error || errData.message || `HTTP ${resp.status}`);
    }
    return resp.json().catch(() => ({}));
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  Fetch helpers (Rich Info)
  // ═══════════════════════════════════════════════════════════════════════════

  /** Fetch stock item with full part + location detail */
  async function _fetchStockItemInfo(stockItemId) {
    try {
      const resp = await fetch(
        `/api/stock/${stockItemId}/?part_detail=true&location_detail=true`, {
        headers: { 'Accept': 'application/json' },
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      return {
        stock_id:     data.pk,
        part_id:      data.part,
        part_name:    data.part_detail?.full_name || data.part_detail?.name || `Part #${data.part}`,
        part_image:   data.part_detail?.thumbnail || data.part_detail?.image || '',
        quantity:     data.quantity,
        location:     data.location_detail?.pathstring || data.location_detail?.name || 'No location',
        location_id:  data.location,
        serial:       data.serial || '',
        batch:        data.batch || '',
        status:       data.status_text || '',
      };
    } catch (e) {
      console.error('[PureScan] Failed to fetch stock item:', e);
      return null;
    }
  }

  /** Fetch the last N tracking entries for a stock item */
  async function _fetchTrackingHistory(stockItemId, limit) {
    try {
      const audit = await fetch(
        `/api/audit/?model_type=stockitem&model_id=${stockItemId}&limit=${limit || 3}&ordering=-timestamp`, {
        headers: { 'Accept': 'application/json' },
      });
      if (audit.ok) {
        const data = await audit.json();
        const results = Array.isArray(data) ? data : (data.results || []);
        return results.slice(0, limit || 3).map(entry => ({
          date: entry.timestamp,
          user: entry.actor_username || entry.actor_detail?.username || '?',
          title: entry.event_label || entry.action_label || '',
          notes: entry.notes || '',
        }));
      }
      if (audit.status !== 404) return [];
      const resp = await fetch(
        `/api/stock/track/?item=${stockItemId}&limit=${limit || 3}&ordering=-date`, {
        headers: { 'Accept': 'application/json' },
      });
      if (!resp.ok) return [];
      const data = await resp.json();
      const results = Array.isArray(data) ? data : (data.results || []);
      return results.slice(0, limit || 3).map(entry => ({
        date:  entry.date || '',
        user:  entry.user_detail?.username || entry.user || '?',
        title: entry.title || entry.label || '',
        notes: entry.notes || '',
      }));
    } catch (e) {
      console.warn('[PureScan] Tracking history fetch failed:', e);
      return [];
    }
  }

  /** Fetch location metadata */
  async function _fetchLocationInfo(locationId) {
    try {
      const resp = await fetch(`/api/stock/location/${locationId}/`, {
        headers: { 'Accept': 'application/json' },
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      return {
        id:        data.pk,
        name:      data.name || `Location #${data.pk}`,
        pathstring: data.pathstring || data.name || '',
        description: data.description || '',
        items:     data.items || 0,
      };
    } catch (e) {
      console.error('[PureScan] Failed to fetch location:', e);
      return null;
    }
  }

  /** Fetch all stock items at a given location */
  async function _fetchLocationInventory(locationId) {
    try {
      const resp = await fetch(
        `/api/stock/?location=${locationId}&part_detail=true&limit=100`, {
        headers: { 'Accept': 'application/json' },
      });
      if (!resp.ok) return [];
      const data = await resp.json();
      const results = Array.isArray(data) ? data : (data.results || []);
      return results.map(item => ({
        stock_id:   item.pk,
        part_id:    item.part,
        part_name:  item.part_detail?.full_name || item.part_detail?.name || `Part #${item.part}`,
        part_image: item.part_detail?.thumbnail || item.part_detail?.image || '',
        quantity:   item.quantity,
        serial:     item.serial || '',
        batch:      item.batch || '',
        status:     item.status_text || '',
      }));
    } catch (e) {
      console.warn('[PureScan] Location inventory fetch failed:', e);
      return [];
    }
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  Action handlers
  // ═══════════════════════════════════════════════════════════════════════════

  /** Rich info display with tracking (stock item) */
  async function _execInfo(stockItemId, info) {
    const tracking = await _fetchTrackingHistory(stockItemId, 3);
    _setState(STATES.SUCCESS, '', {
      richInfo: { info, tracking },
    });
    clearTimeout(_resultTimer);
    _resultTimer = setTimeout(() => _resetToIdle(), 10000);
  }

  /** Location inventory display */
  async function _execLocationInfo(locationId) {
    _setState(STATES.PROCESSING, 'Loading location inventory…');
    const [locInfo, inventory] = await Promise.all([
      _fetchLocationInfo(locationId),
      _fetchLocationInventory(locationId),
    ]);
    if (!locInfo) {
      _showResult(STATES.ERROR, `Location #${locationId} not found`);
      return;
    }
    _setState(STATES.SUCCESS, '', {
      locationInfo: { location: locInfo, inventory },
    });
    clearTimeout(_resultTimer);
    _resultTimer = setTimeout(() => _resetToIdle(), 20000);
  }

  /** Execute: Part info (native InvenTree part label) */
  async function _execPartInfo(partId) {
    _setState(STATES.PROCESSING, 'Looking up part.');
    try {
      const resp = await fetch(`/api/part/${partId}/`, {
        headers: { Accept: 'application/json' },
      });
      if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
      const data = await resp.json();
      const lines = [
        `Part: ${data.name || data.full_name || ('Part #' + data.pk)}`,
        data.IPN ? `IPN: ${data.IPN}` : '',
        data.description ? `Desc: ${String(data.description).slice(0, 80)}` : '',
        data.category_detail ? `Category: ${data.category_detail.name}` : '',
        `Stock items: ${data.in_stock ?? '?'}`,
      ].filter(Boolean);
      _showResult(STATES.SUCCESS, lines.join('\n'), 8000);
    } catch (e) {
      _showResult(STATES.ERROR, `Part lookup failed: ${e.message}`);
    }
  }

  /** Execute: Transfer stock item to a new location */
  async function _execTransfer(stockItemId, locationId) {
    const prevLocId   = _itemInfo?.location_id;
    const prevLocName = _itemInfo?.location;
    _setState(STATES.PROCESSING, 'Transferring stock item…');
    try {
      await _apiPost('/api/stock/transfer/', {
        items: [{ pk: stockItemId, quantity: _itemInfo?.quantity }],
        location: locationId,
        notes: `PureScan: Transferred "${_itemInfo?.part_name}" from ${_itemInfo?.location}`,
      });
      const updatedInfo = await _fetchStockItemInfo(stockItemId);
      const locName = updatedInfo?.location || `Location #${locationId}`;
      const desc = `Transferred "${_itemInfo?.part_name}" → ${locName}`;
      _pushLog({
        ts: new Date(),
        description: desc,
        actionType: 'TRANSFER',
        undone: false,
        payload: {
          stockItemId,
          quantity: _itemInfo?.quantity,
          partName: _itemInfo?.part_name,
          toLocationId: locationId,
          toLocation: locName,
          fromLocationId: prevLocId,
          fromLocation: prevLocName,
        },
      });

      // ── Auto-print label for the transferred item ──
      let printMsg = '';
      try {
        const printResult = await _apiPost('/plugin/smartparts/api/purescan/print/', {
          stock_item_id: stockItemId,
        });
        printMsg = printResult.success
          ? '\n🖨 Label sent to printer'
          : `\n⚠ Label print failed: ${printResult.error || 'unknown'}`;
      } catch (printErr) {
        printMsg = `\n⚠ Label print skipped: ${printErr.message}`;
      }

      _showResult(STATES.SUCCESS, `✓ ${desc}${printMsg}`);
    } catch (e) {
      _showResult(STATES.ERROR, `Transfer failed: ${e.message}`);
    }
  }

  // ── v3: distributor label lookup (used by INFO / CREATESTOCK / DELETEPART) ──

  async function _lookupDistributorBarcode(raw) {
    try {
      return await _apiPost('/plugin/smartparts/api/barcode/lookup/', { barcode: raw });
    } catch (e) {
      return { error: e.message };
    }
  }

  const DISTRIBUTOR_LABELS = { mouser: 'Mouser', digikey: 'DigiKey' };

  // "✓ Filled: a, b" / "Nothing to fill" line for gap-fill results.
  // Errors and pricing warnings are listed separately (_warningLines).
  function _filledNote(resp) {
    if (!resp) return '';
    const filled = resp.enriched || [];
    if (filled.length) return `✓ Filled: ${filled.join(', ')}`;
    if (resp.enrich_skipped) return `Not filled: ${resp.enrich_skipped}`;
    if ((resp.enrich_errors || []).length) return '';
    return 'Nothing to fill';
  }

  // "\n⚠ …" lines for server warnings (e.g. supplier pricing skipped)
  function _warningLines(messages) {
    return (messages || []).map(message => `\n⚠ ${message}`).join('');
  }

  function _formatDistributorInfo(lk) {
    const d = lk.barcode_data || {};
    const lines = [];
    const dist = d.distributor || lk.distributor;
    if (dist) lines.push(`Label: ${DISTRIBUTOR_LABELS[dist] || dist}`);
    lines.push(`MPN: ${d.mpn || lk.mpn || '—'}`);
    if (d.manufacturer) lines.push(`Mfr: ${d.manufacturer}`);
    if (d.supplier_sku) lines.push(`SKU: ${d.supplier_sku}`);
    if (d.quantity != null) lines.push(`Qty: ${d.quantity}`);
    if (d.batch) lines.push(`Batch: ${d.batch}`);
    if (lk.found) {
      lines.push(`Part in DB: YES — ${lk.part_name || ''}${lk.part_ipn ? ' (' + lk.part_ipn + ')' : ''}`);
    } else {
      lines.push('Part in DB: NO');
    }
    return lines.join('\n');
  }

  function _showDistributorInfo(lk) {
    const label = lk.found ? '📦 Distributor label' : '📦 Distributor label (not in DB)';
    _setState(STATES.SUCCESS, `${label}\n${_formatDistributorInfo(lk)}`);
    clearTimeout(_resultTimer);
    _resultTimer = setTimeout(() => _resetToIdle(), 12000);
  }

  async function _execDistributorStockInfo(lk, flow) {
    const query = new URLSearchParams({ part_id: lk.part_id });
    const last = _getLastLocation();
    if (last) query.set('location_id', last.id);
    _setState(STATES.PROCESSING, 'Looking up stock…');
    try {
      const response = await fetch(`/plugin/smartparts/api/purescan/stock-summary/?${query}`);
      const data = await response.json();
      if (!response.ok) throw new Error(data.error || `HTTP ${response.status}`);
      // A cancel or new command can arrive while the request is in flight.
      if (flow !== _flowId) return;
      let detail = `Total stock: ${data.total_stock}`;
      if (data.location_stock != null) {
        detail += `\nIn location: ${data.location_stock}`;
        const labels = data.stock_labels || [];
        if (labels.length) detail += ` [${labels.join(', ')}${data.more_labels ? ', ...' : ''}]`;
      }
      _showResult(STATES.SUCCESS, detail, 12000);
    } catch (e) {
      if (flow === _flowId) {
        _showResult(STATES.ERROR, `Stock lookup failed: ${e.message}`);
      }
    }
  }

  // ── v3: SYS:LAST / CREATESTOCK helpers ──

  function _locationPrompt(contextText) {
    const last = _getLastLocation();
    if (last && last.id) {
      return `${contextText}\n📍 Location: ${last.name} (SYS:LAST)\n→ Scan a Location label to override, or SYS:QTY:nnn to keep`;
    }
    return `${contextText}\n📍 No default location — scan a Location label`;
  }

  function _enterCreateStockQty() {
    if (_pendingQty != null && _pendingQty > 0) {
      _setState(STATES.AWAITING_QTY,
        `Quantity: ${_pendingQty}\n→ SYS:QTY:nnn to change, or scan a FREE STOCK_ token to accept`);
    } else {
      _setState(STATES.AWAITING_QTY, '→ Scan a Quantity Code (SYS:QTY:nnn)');
    }
  }

  // ── v3: CREATEPART ──

  async function _execCreatePart(raw) {
    _setState(STATES.PROCESSING, 'Searching distributors… (this can take a few seconds)');
    try {
      const resp = await _apiPost('/plugin/smartparts/api/purescan/createpart/', { barcode: raw });
      if (resp.already_exists) {
        const note = _filledNote(resp);
        const warnings = _warningLines(resp.enrich_errors);
        _showResult(STATES.SUCCESS,
          `⚠ Part already exists: ${resp.part_name || ''}${resp.part_ipn ? ' (' + resp.part_ipn + ')' : ''}${note ? '\n' + note : ''}${warnings}`,
          warnings ? 12000 : 8000);
        return;
      }
      if (resp.success) {
        const catNote = resp.category_path
          ? `\n→ Category: ${resp.category_path}${resp.category_created ? ' (auto-created)' : ''}`
          : '';
        const warnings = _warningLines(resp.errors);
        _showResult(STATES.SUCCESS,
          `✓ Part created: ${resp.part_name || '#' + resp.part_id}${catNote}\n(no stock yet — use SYS:CREATESTOCK)${warnings}`, warnings ? 12000 : 6000);
      } else {
        _showResult(STATES.ERROR, `✗ ${resp.message || resp.error || 'Part creation failed'}`, 8000);
      }
    } catch (e) {
      _showResult(STATES.ERROR, `Create part failed: ${e.message}`, 6000);
    }
  }

  // ── v3: CREATESTOCK ──

  async function _execCreateStockStart(raw) {
    const bc = await _parseInvenTreeBarcode(raw);

    // Mode B: existing stock item token → part inferred (token is read-only)
    if (bc.type === 'stockitem' && bc.id) {
      _setState(STATES.PROCESSING, 'Looking up part from stock item…');
      const info = await _fetchStockItemInfo(bc.id);
      if (!info) {
        _showResult(STATES.ERROR, `Stock item #${bc.id} not found`);
        return;
      }
      _pendingPartId = info.part_id;
      _pendingPartName = info.part_name;
      _pendingQty = null;
      _pendingBatch = '';
      _pendingBarcodeData = null;
      _setState(STATES.AWAITING_LOCATION, _locationPrompt(`Create stock for "${info.part_name}"`));
      return;
    }

    // Alternative: InvenTree part label
    if (bc.type === 'part' && bc.id) {
      _pendingPartId = bc.id;
      _pendingPartName = bc.name || `Part #${bc.id}`;
      _pendingQty = null;
      _pendingBatch = '';
      _pendingBarcodeData = null;
      _setState(STATES.AWAITING_LOCATION, _locationPrompt(`Create stock for "${_pendingPartName}"`));
      return;
    }

    // Mode A: distributor DataMatrix
    if (!bc.type) {
      _setState(STATES.PROCESSING, 'Looking up part…');
      const lk = await _lookupDistributorBarcode(raw);
      if (lk.error) {
        _showResult(STATES.ERROR, `Part lookup failed: ${lk.error}`);
        return;
      }
      if (!lk || (!lk.found && !lk.mpn)) {
        _showResult(STATES.ERROR, 'Unknown part — run SYS:CREATEPART first', 6000);
        return;
      }
      if (!lk.found) {
        _showResult(STATES.ERROR, `Part not in DB (MPN ${lk.mpn || '?'}) — run SYS:CREATEPART first`, 6000);
        return;
      }
      const d = lk.barcode_data || {};
      _pendingPartId = lk.part_id;
      _pendingPartName = lk.part_name || '';
      // Mode A: qty from the label's Q field, default 1 (spec: "default quantity = 1")
      _pendingQty = d.quantity != null ? parseFloat(d.quantity) : 1;
      _pendingBatch = d.batch || '';
      _pendingBarcodeData = d;
      _setState(STATES.AWAITING_LOCATION,
        _locationPrompt(`Create stock for "${_pendingPartName}"${_pendingQty ? ' (reel qty ' + _pendingQty + ')' : ''}`));
      return;
    }

    _setState(STATES.AWAITING_ITEM,
      '⚠ Scan a distributor label, an existing STOCK_ token, or a part label.\nAction: CREATESTOCK');
  }

  async function _execCreateStockCommit(token) {
    const flow = _flowId;
    const retryState = _state;
    const desc = `Created stock "${_pendingPartName}" qty ${_pendingQty}` +
      (_pendingBatch ? ` batch ${_pendingBatch}` : '') + ` → ${token}`;
    const msg = `✓ ${desc}\nStick the label on the container${_pendingPricingNote}`;
    _setState(STATES.PROCESSING, 'Creating stock item…');
    try {
      await _apiPost('/plugin/smartparts/api/purescan/createstock/', {
        part_id: _pendingPartId,
        location_id: _pendingLocationId,
        quantity: _pendingQty,
        batch: _pendingBatch || '',
        new_token: token,
      });
      _pushLog({
        ts: new Date(), description: desc, actionType: 'CREATESTOCK', undone: false,
        payload: null,
      });
      if (flow !== _flowId) return;
      if (_action === ACTIONS.FASTADD) {
        _startBurst(msg);
      } else {
        _showResult(STATES.SUCCESS, msg, 8000);
      }
      _clearPendingCreate();
    } catch (e) {
      if (flow !== _flowId) return;
      _setState(retryState, `⚠ Create stock failed: ${e.message}\nScan a free STOCK_ token, or SYS:CANCEL`);
    }
  }

  // ── v3: FASTADD ──

  async function _execFastAddStart(raw) {
    const flow = _flowId;
    _setState(STATES.PROCESSING, 'Reading label…');
    const bc = await _parseInvenTreeBarcode(raw);
    if (flow !== _flowId) return;

    // Instance label → SYS:ADD combo flow
    if (bc.type === 'stockitem' && bc.id) {
      _action = ACTIONS.ADD;
      _stockItemId = bc.id;
      _setState(STATES.PROCESSING, 'Looking up stock item…');
      const info = await _fetchStockItemInfo(bc.id);
      if (flow !== _flowId) return;
      if (!info) {
        _showResult(STATES.ERROR, `Stock Item #${bc.id} not found`);
        return;
      }
      _itemInfo = info;
      _setState(STATES.AWAITING_PARAM,
        `${_action}: "${info.part_name}" (current: ${info.quantity}×)\n▶ Scan a Quantity Code (SYS:QTY:nnn) or scan item again for +1`);
      return;
    }

    // Location / part labels → error, stay in AWAITING_ITEM
    if (bc.type === 'stocklocation' || bc.type === 'part') {
      _setState(STATES.AWAITING_ITEM,
        `⚠ That is a ${bc.type} label.\n▶ Scan a distributor DataMatrix or an instance (STOCK_) label.\nAction: FASTADD`);
      return;
    }

    // Distributor DataMatrix → resolve part (auto-create if not in DB)
    if (!bc.type) {
      _setState(STATES.PROCESSING, 'Looking up part… (this can take a few seconds)');
      const lk = await _lookupDistributorBarcode(raw);
      if (flow !== _flowId) return;
      if (lk.error) {
        _showResult(STATES.ERROR, `Part lookup failed: ${lk.error}`);
        return;
      }
      if (!lk || (!lk.found && !lk.mpn)) {
        _showResult(STATES.ERROR, 'Unknown part — run SYS:CREATEPART first', 6000);
        return;
      }
      if (!lk.found) {
        // Part not in DB → create it (like SYS:CREATEPART)
        try {
          const resp = await _apiPost('/plugin/smartparts/api/purescan/createpart/', { barcode: raw });
          if (flow !== _flowId) return;
          if (resp.success) {
            _pendingPartId = resp.part_id;
            _pendingPartName = resp.part_name || '';
            _pendingCategoryNote = resp.category_path
              ? `${resp.category_path}${resp.category_created ? ' (auto-created)' : ''}`
              : '';
            _pendingPricingNote = _warningLines(resp.errors);
          } else if (resp.already_exists) {
            // Created meanwhile / found by canonical MPN: the server gap-filled it
            _pendingPartId = resp.part_id;
            _pendingPartName = resp.part_name || '';
            if ((resp.enriched || []).length) _pendingCategoryNote = _filledNote(resp);
            _pendingPricingNote = _warningLines(resp.enrich_errors);
          } else {
            _showResult(STATES.ERROR, `⛔ ${resp.message || resp.error || 'Part creation failed'}`, 8000);
            return;
          }
        } catch (e) {
          if (flow !== _flowId) return;
          _showResult(STATES.ERROR, `Part creation failed: ${e.message}`, 6000);
          return;
        }
      } else {
        _pendingPartId = lk.part_id;
        _pendingPartName = lk.part_name || '';
        // One-time gap-fill: the label's distributor (e.g. Mouser) has no
        // supplier part on this part yet → add it and fill empty fields.
        // Later scans of the same part skip this (no API call).
        if (lk.distributor && lk.distributor_supplier_part === false) {
          _setState(STATES.PROCESSING,
            `Adding ${DISTRIBUTOR_LABELS[lk.distributor] || lk.distributor} data to "${_pendingPartName}"…`);
          try {
            const er = await _apiPost('/plugin/smartparts/api/purescan/createpart/',
              { barcode: raw, enrich_scope: 'label' });
            // A cancel or new command can arrive while the gap-fill runs.
            if (flow !== _flowId) return;
            if (er && er.already_exists && (er.enriched || []).length) {
              _pendingCategoryNote = _filledNote(er);
            }
            _pendingPricingNote = _warningLines(er && er.enrich_errors);
          } catch (e) {
            if (flow !== _flowId) return;
            console.warn('[PureScan] FASTADD gap-fill failed:', e);
          }
        }
      }
      const d = lk.barcode_data || {};
      _pendingQty = d.quantity != null ? parseFloat(d.quantity) : 1;
      _pendingBatch = d.batch || '';
      _pendingBarcodeData = d;
      _setState(STATES.AWAITING_LOCATION,
        _locationPrompt(`Create stock for "${_pendingPartName}"${_pendingCategoryNote ? ' — ' + _pendingCategoryNote : ''}${_pendingQty ? ' (reel qty ' + _pendingQty + ')' : ''}${_pendingPricingNote}`));
      return;
    }

    _setState(STATES.AWAITING_ITEM,
      '⚠ Scan a distributor DataMatrix or an instance (STOCK_) label.\nAction: FASTADD');
  }

  async function _execFastAddMerge(bc) {
    const flow = _flowId;
    const qty = _pendingQty;
    const partName = _pendingPartName;
    _setState(STATES.PROCESSING, `Adding ${_pendingQty}× to "${_pendingPartName}"…`);
    try {
      await _apiPost('/api/stock/add/', {
        items: [{ pk: bc.id, quantity: qty }],
        notes: `PureScan FASTADD: merge ${qty}×`,
      });
      const desc = `Added ${qty}× "${partName}" to existing instance ${bc.token || '#' + bc.id}`;
      _pushLog({
        ts: new Date(), description: desc, actionType: 'ADD', undone: false,
        payload: { stockItemId: bc.id, quantity: qty, partName, previousQty: null },
      });
      if (flow !== _flowId) return;
      const msg = `✅ ${desc}\nMerged into existing instance${_pendingPricingNote}`;
      if (_action === ACTIONS.FASTADD) {
        _startBurst(msg);
      } else {
        _showResult(STATES.SUCCESS, msg, 8000);
      }
      _clearPendingCreate();
    } catch (e) {
      if (flow !== _flowId) return;
      _showResult(STATES.ERROR, `Merge failed: ${e.message}`, 8000);
    }
  }

  // ── v3: DELETEPART ──

  async function _execDeletePartStart(raw) {
    _setState(STATES.PROCESSING, 'Looking up part…');
    const lk = await _lookupDistributorBarcode(raw);
    if (!lk || !lk.found || !lk.part_id) {
      _showResult(STATES.ERROR, 'Part not found in DB — nothing to delete', 5000);
      return;
    }
    _pendingDeleteKind = 'part';
    _pendingDeleteId = lk.part_id;
    _pendingDeleteDesc = `${lk.part_name}${lk.part_ipn ? ' (' + lk.part_ipn + ')' : ''}`;
    _pendingDeleteRaw = raw;
    _setState(STATES.AWAITING_CONFIRM,
      `⛔ DELETE PART: "${_pendingDeleteDesc}"\nAll its data (params, supplier links) will be removed.\n→ Scan the SAME label again to confirm, or SYS:CANCEL`);
  }

  async function _execDeletePartCommit() {
    _setState(STATES.PROCESSING, 'Deleting part…');
    try {
      const resp = await _apiPost('/plugin/smartparts/api/purescan/deletepart/', {
        part_id: _pendingDeleteId,
        confirm: true,
      });
      _showResult(STATES.SUCCESS, `✓ ${resp.message || 'Part deleted'}`, 6000);
      _pushLog({
        ts: new Date(), description: `Deleted part "${_pendingDeleteDesc}"`,
        actionType: 'DELETEPART', undone: false, payload: null,
      });
    } catch (e) {
      _showResult(STATES.ERROR, `Delete failed: ${e.message}`, 6000);
    }
    _clearPendingDelete();
  }

  // ── v3: DELETESTOCK ──

  async function _execDeleteStockStart(raw) {
    const bc = await _parseInvenTreeBarcode(raw);
    if (bc.type !== 'stockitem' || !bc.id) {
      _setState(STATES.AWAITING_ITEM,
        '⚠ Scan a STOCK_XXXX token bound to a stock item.\nAction: DELETESTOCK');
      return;
    }
    const info = await _fetchStockItemInfo(bc.id);
    if (!info) {
      _showResult(STATES.ERROR, `Stock item #${bc.id} not found`);
      return;
    }
    _pendingDeleteKind = 'stock';
    _pendingDeleteId = bc.id;
    _pendingToken = bc.token || null;
    _pendingDeleteDesc = `${info.part_name} (${info.quantity}× @ ${info.location})`;
    _setState(STATES.AWAITING_CONFIRM,
      `⛔ DELETE STOCK: "${_pendingDeleteDesc}"\nThe token will be freed for reuse.\n→ Scan the SAME token again to confirm, or SYS:CANCEL`);
  }

  async function _execDeleteStockCommit(token) {
    _setState(STATES.PROCESSING, 'Deleting stock item…');
    try {
      const resp = await _apiPost('/plugin/smartparts/api/purescan/deletestock/', {
        token: token || null,
        stock_id: _pendingDeleteId,
        confirm: true,
      });
      _showResult(STATES.SUCCESS, `✓ ${resp.message || 'Stock item deleted'}`, 6000);
      _pushLog({
        ts: new Date(), description: `Deleted stock "${_pendingDeleteDesc}" (${token || 'token'} freed)`,
        actionType: 'DELETESTOCK', undone: false, payload: null,
      });
    } catch (e) {
      _showResult(STATES.ERROR, `Delete failed: ${e.message}`, 6000);
    }
    _clearPendingDelete();
  }

  // ── Combo-timer execution ─────────────────────────────────────────────────

  function _startCombo(qty) {
    clearTimeout(_comboTimer);
    _comboQty = qty;
    _setState(STATES.COMBO,
      `${_action}: "${_itemInfo?.part_name}"\nQuantity: ${_comboQty}   ⏳ Scan more QTY codes or wait 3s…`);
    _comboTimer = setTimeout(() => _executeCombo(), COMBO_TIMEOUT_MS);
  }

  function _addCombo(qty) {
    clearTimeout(_comboTimer);
    _comboQty += qty;
    _setState(STATES.COMBO,
      `${_action}: "${_itemInfo?.part_name}"\nQuantity: ${_comboQty}   ⏳ Scan more QTY codes or wait 3s…`);
    _comboTimer = setTimeout(() => _executeCombo(), COMBO_TIMEOUT_MS);
  }

  async function _executeCombo() {
    _comboTimer = null;
    const qty = _comboQty;
    const action = _action;
    const itemId = _stockItemId;
    const info = _itemInfo;

    if (action === ACTIONS.ADD) {
      _setState(STATES.PROCESSING, `Adding ${qty}× to "${info?.part_name}"…`);
      try {
        await _apiPost('/api/stock/add/', {
          items: [{ pk: itemId, quantity: qty }],
          notes: `PureScan: Add ${qty}`,
        });
        const newQty = (info?.quantity || 0) + qty;
        const desc = `Added ${qty}× "${info?.part_name}" (now ${newQty})`;
        _pushLog({
          ts: new Date(), description: desc, actionType: 'ADD', undone: false,
          payload: { stockItemId: itemId, quantity: qty, partName: info?.part_name, previousQty: info?.quantity },
        });
        _showResult(STATES.SUCCESS, `✓ ${desc}`);
      } catch (e) {
        _showResult(STATES.ERROR, `Add stock failed: ${e.message}`);
      }

    } else if (action === ACTIONS.REMOVE) {
      // ── Depletion handling: removing to zero deletes the stock item ──
      const currentQty = info?.quantity || 0;
      if (qty > currentQty) {
        _showResult(STATES.ERROR,
          `⛔ Cannot remove ${qty}× — only ${currentQty}× available.\n"${info?.part_name}" has less stock than requested.`, 6000);
        return;
      }
      if (qty === currentQty) {
        // Full depletion → the stock item is deleted (label/token freed).
        // Reuse the double-scan confirm gate (DELETESTOCK machinery).
        _pendingDeleteKind = 'stock';
        _pendingDeleteId = itemId;
        _pendingToken = null;
        _pendingDeleteDesc = `Depleting ${info?.part_name} (${qty}× @ ${info?.location})`;
        _setState(STATES.AWAITING_CONFIRM,
          `⚠ DEPLETING: "${info?.part_name}" — removing ${qty}× leaves 0\nThe stock item will be DELETED (label freed for reuse).\n▶ Scan the SAME label again to confirm, or SYS:CANCEL`);
        return;
      }

      _setState(STATES.PROCESSING, `Removing ${qty}× from "${info?.part_name}"…`);
      try {
        await _apiPost('/api/stock/remove/', {
          items: [{ pk: itemId, quantity: qty }],
          notes: `PureScan: Remove ${qty}`,
        });
        const newQty = Math.max(0, currentQty - qty);
        const desc = `Removed ${qty}× "${info?.part_name}" (now ${newQty})`;
        _pushLog({
          ts: new Date(), description: desc, actionType: 'REMOVE', undone: false,
          payload: { stockItemId: itemId, quantity: qty, partName: info?.part_name, previousQty: info?.quantity },
        });
        _showResult(STATES.SUCCESS, `✓ ${desc}`);
      } catch (e) {
        _showResult(STATES.ERROR, `Remove stock failed: ${e.message}`);
      }

    } else if (action === ACTIONS.STOCKTAKE) {
      _setState(STATES.PROCESSING, `Setting count to ${qty} for "${info?.part_name}"…`);
      try {
        await _apiPost('/api/stock/count/', {
          items: [{ pk: itemId, quantity: qty }],
          notes: `PureScan: Stocktake = ${qty}`,
        });
        const desc = `Stocktake: "${info?.part_name}" counted as ${qty}`;
        _pushLog({
          ts: new Date(), description: desc, actionType: 'STOCKTAKE', undone: false,
          payload: { stockItemId: itemId, quantity: qty, partName: info?.part_name, previousQty: info?.quantity },
        });
        _showResult(STATES.SUCCESS, `✓ ${desc}`);
      } catch (e) {
        _showResult(STATES.ERROR, `Stocktake failed: ${e.message}`);
      }
    }
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  Main scan handler
  // ═══════════════════════════════════════════════════════════════════════════

  async function handleScan(rawString) {
    const raw = (rawString || '').trim();
    if (!raw) return;
    if (_state === STATES.PROCESSING && !/^SYS:/i.test(raw)) return;
    const scanFlow = _flowId;
    if (/^SYS:/i.test(raw) && _burstTimer !== null) _stopBurst();

    // ── Global: SYS:CANCEL always resets ─────────────────────────────────
    if (raw.toUpperCase() === 'SYS:CANCEL' || raw.toUpperCase() === 'SYS:RESET') {
      clearTimeout(_comboTimer);
      _resetToIdle('Cancelled — scan a new Action Code');
      return;
    }

    // ── Global: SYS:CLEARLOG clears the action log ──────────────────────
    if (raw.toUpperCase() === 'SYS:CLEARLOG' || raw.toUpperCase() === 'SYS:CLEAR') {
      _flowId++;
      clearLog();
      _showResult(STATES.SUCCESS, '🗑 Action log cleared', 2000);
      return;
    }

    // ── Global: SYS:LAST sets the terminal default location ─────────────
    if (raw.toUpperCase() === 'SYS:LAST') {
      _flowId++;
      clearTimeout(_resultTimer);
      clearTimeout(_comboTimer);
      _clearPendingCreate();
      _clearPendingDelete();
      _action = ACTIONS.LAST;
      const last = _getLastLocation();
      _setState(STATES.AWAITING_LOCATION,
        `📍 Current default location: ${last ? last.name : 'none'}\n→ Scan a Location label to set it`);
      return;
    }

    // ── Global: SYS:UNDO ─────────────────────────────────────────────────
    const ctrlCode = _parseControlCode(raw);
    if (ctrlCode === '__UNDO__') {
      _flowId++;
      clearTimeout(_comboTimer);
      await _executeUndo();
      return;
    }

    if (_state === STATES.PROCESSING && ctrlCode) {
      _armAction(ctrlCode);
      return;
    }

    // ── STATE: COMBO — accumulating qty scans ────────────────────────────
    if (_state === STATES.COMBO) {
      // Action code switches abort combo and start new flow
      if (ctrlCode && ctrlCode !== '__UNDO__') {
        clearTimeout(_comboTimer);
        _comboQty = 0;
        _action = ctrlCode;
        _stockItemId = null;
        _itemInfo = null;
        _setState(STATES.AWAITING_ITEM,
          `Action: ${ctrlCode} — ${_itemPrompt(ctrlCode)}`);
        return;
      }

      // Quantity code: accumulate
      const qty = _parseQuantityCode(raw);
      if (qty !== null && qty > 0) {
        if (_action === ACTIONS.STOCKTAKE) {
          // For stocktake, QTY replaces, not adds
          _startCombo(qty);
        } else {
          _addCombo(qty);
        }
        return;
      }

      // Re-scanning the same stock item → add 1 more
      const bc = await _parseInvenTreeBarcode(raw);
      if (bc.type === 'stockitem' && bc.id === _stockItemId) {
        if (_action !== ACTIONS.STOCKTAKE) {
          _addCombo(1);
        }
        return;
      }

      // Unknown scan during combo — ignore
      return;
    }

    // ── STATE: IDLE / SUCCESS / ERROR ────────────────────────────────────
    if (_state === STATES.IDLE || _state === STATES.SUCCESS || _state === STATES.ERROR) {
      const burstActive = _burstTimer != null && Date.now() < _burstDeadline && _action === ACTIONS.FASTADD;
      clearTimeout(_resultTimer);
      _stopBurst();

      // v1.2.6 Burst: during the window, route scans straight into FASTADD
      if (burstActive && _state === STATES.SUCCESS && !ctrlCode) {
        await _execFastAddStart(raw);
        return;
      }

      if (ctrlCode) {
        _armAction(ctrlCode);
        return;
      }

      // Quick-scan shortcut: stock item in IDLE → Info
      const bc = await _parseInvenTreeBarcode(raw);
      if (bc.type === 'stockitem' && bc.id) {
        _action = ACTIONS.INFO;
        _stockItemId = bc.id;
        _setState(STATES.PROCESSING, 'Looking up stock item…');
        const info = await _fetchStockItemInfo(bc.id);
        if (!info) {
          _showResult(STATES.ERROR, `Stock Item #${bc.id} not found`);
          return;
        }
        _itemInfo = info;
        await _execInfo(bc.id, info);
        return;
      }

      // Quick-scan shortcut: location in IDLE → Location Info
      if (bc.type === 'stocklocation' && bc.id) {
        _action = ACTIONS.INFO;
        await _execLocationInfo(bc.id);
        return;
      }

      // Quick-scan shortcut: part label in IDLE → Part Info
      if (bc.type === 'part' && bc.id) {
        _action = ACTIONS.INFO;
        await _execPartInfo(bc.id);
        return;
      }

      // Quick-scan shortcut: free token in IDLE → token info
      if (bc.type === 'free') {
        _showResult(STATES.SUCCESS,
          `🏷 Free token ${bc.token}\nUsable in SYS:CREATESTOCK to bind a new instance`, 6000);
        return;
      }

      // Quick-scan shortcut: distributor DataMatrix in IDLE → part lookup
      if (!bc.type) {
        const lk = await _lookupDistributorBarcode(raw);
        if (lk.error) {
          _showResult(STATES.ERROR, `Part lookup failed: ${lk.error}`);
          return;
        }
        if (lk && (lk.found || (lk.barcode_data && lk.barcode_data.mpn))) {
          _showDistributorInfo(lk);
          return;
        }
      }

      _showResult(STATES.ERROR,
        `Unknown scan: "${raw.slice(0, 40)}"\nScan an Action Code to begin`, 3000);
      return;
    }

    // ── STATE: AWAITING_ITEM ─────────────────────────────────────────────
    if (_state === STATES.AWAITING_ITEM) {
      if (ctrlCode) {
        _armAction(ctrlCode);
        return;
      }

      // v3: slow-workflow actions route to their dedicated handlers
      if (_action === ACTIONS.CREATEPART) {
        await _execCreatePart(raw);
        return;
      }
      if (_action === ACTIONS.DELETEPART) {
        await _execDeletePartStart(raw);
        return;
      }
      if (_action === ACTIONS.DELETESTOCK) {
        await _execDeleteStockStart(raw);
        return;
      }
      if (_action === ACTIONS.CREATESTOCK) {
        await _execCreateStockStart(raw);
        return;
      }
      if (_action === ACTIONS.FASTADD) {
        await _execFastAddStart(raw);
        return;
      }

      const infoFlow = _action === ACTIONS.INFO ? _flowId : null;
      if (infoFlow !== null) _setState(STATES.PROCESSING, 'Reading label…');
      const bc = await _parseInvenTreeBarcode(raw);
      if (infoFlow !== null && infoFlow !== _flowId) return;

      // INFO mode also accepts location barcodes
      if (_action === ACTIONS.INFO && bc.type === 'stocklocation' && bc.id) {
        await _execLocationInfo(bc.id);
        return;
      }

      // INFO mode also accepts part labels
      if (_action === ACTIONS.INFO && bc.type === 'part' && bc.id) {
        await _execPartInfo(bc.id);
        return;
      }

      // INFO mode also accepts distributor DataMatrix labels
      if (_action === ACTIONS.INFO && !bc.type) {
        _setState(STATES.PROCESSING, 'Looking up distributor label…');
        const lk = await _lookupDistributorBarcode(raw);
        if (infoFlow !== _flowId) return;
        if (lk.error) {
          _showResult(STATES.ERROR, `Part lookup failed: ${lk.error}`);
          return;
        }
        if (lk?.found && lk.part_id) {
          await _execDistributorStockInfo(lk, infoFlow);
          return;
        }
        if (lk && (lk.found || (lk.barcode_data && lk.barcode_data.mpn))) {
          _showDistributorInfo(lk);
          return;
        }
      }

      if (bc.type !== 'stockitem' || !bc.id) {
        _setState(STATES.AWAITING_ITEM,
          `⚠ Not a Stock Item label! Scan a valid Stock Item${_action === ACTIONS.INFO ? ' or Location' : ''}.\nAction: ${_action}`);
        return;
      }

      _stockItemId = bc.id;
      _setState(STATES.PROCESSING, 'Looking up stock item…');
      const info = await _fetchStockItemInfo(bc.id);
      if (!info) {
        _showResult(STATES.ERROR, `Stock Item #${bc.id} not found in InvenTree`);
        return;
      }
      _itemInfo = info;

      // Route based on action
      if (_action === ACTIONS.INFO) {
        await _execInfo(bc.id, info);
        return;
      }

      if (_action === ACTIONS.TRANSFER) {
        _setState(STATES.AWAITING_PARAM,
          `Transfer: "${info.part_name}" (${info.quantity}×)\n📍 From: ${info.location}\n→ Now scan the DESTINATION Location label`);
        return;
      }

      if (_action === ACTIONS.ADD || _action === ACTIONS.REMOVE || _action === ACTIONS.STOCKTAKE) {
        // Enter combo mode with qty=0 — wait for first qty scan
        _setState(STATES.AWAITING_PARAM,
          `${_action}: "${info.part_name}" (current: ${info.quantity}×)\n→ Scan a Quantity Code (SYS:QTY:nnn) or scan item again for +1`);
        return;
      }

      _showResult(STATES.ERROR, `Unknown action: ${_action}`);
      return;
    }

    // ── STATE: AWAITING_PARAM ────────────────────────────────────────────
    if (_state === STATES.AWAITING_PARAM) {
      if (ctrlCode) {
        _action = ctrlCode;
        _stockItemId = null;
        _itemInfo = null;
        _setState(STATES.AWAITING_ITEM,
          `Action: ${ctrlCode} — ${_itemPrompt(ctrlCode)}`);
        return;
      }

      const bc = await _parseInvenTreeBarcode(raw);

      // TRANSFER: needs a location
      if (_action === ACTIONS.TRANSFER) {
        if (bc.type === 'stocklocation' && bc.id) {
          await _execTransfer(_stockItemId, bc.id);
          return;
        }
        _setState(STATES.AWAITING_PARAM,
          `⚠ Not a Location label! Scan a Location barcode.\nTransfer: "${_itemInfo?.part_name}" → ???`);
        return;
      }

      // ADD / REMOVE / STOCKTAKE: enter combo mode
      if (_action === ACTIONS.ADD || _action === ACTIONS.REMOVE || _action === ACTIONS.STOCKTAKE) {
        // Quantity code starts combo
        const qty = _parseQuantityCode(raw);
        if (qty !== null && qty > 0) {
          _startCombo(qty);
          return;
        }

        // Re-scanning the stock item → combo with +1
        if (bc.type === 'stockitem' && bc.id === _stockItemId) {
          if (_action !== ACTIONS.STOCKTAKE) {
            _startCombo(1);
          }
          return;
        }

        _setState(STATES.AWAITING_PARAM,
          `⚠ Invalid quantity! Scan a QTY code (SYS:QTY:nnn) or scan item for +1\n${_action}: "${_itemInfo?.part_name}"`);
        return;
      }
    }

    // ── STATE: AWAITING_LOCATION ─────────────────────────────────────────
    if (_state === STATES.AWAITING_LOCATION) {
      if (ctrlCode) {
        if (ctrlCode === ACTIONS.LAST) {
          if (_action === ACTIONS.LAST) {
            const last = _getLastLocation();
            _setState(STATES.AWAITING_LOCATION,
              `📍 Current default: ${last ? last.name : 'none'}\n→ Scan a Location label to set`);
            return;
          }
          // CREATESTOCK/FASTADD: accept the stored default location
          const last = _getLastLocation();
          if (last && last.id) {
            _pendingLocationId = last.id;
            _enterCreateStockQty();
            return;
          }
          _setState(STATES.AWAITING_LOCATION,
            '⚠ No default location stored — scan a Location label');
          return;
        }
        _armAction(ctrlCode);
        return;
      }

      const bc = await _parseInvenTreeBarcode(raw);
      if (scanFlow !== _flowId || _state !== STATES.AWAITING_LOCATION) return;

      if (bc.type === 'stocklocation' && bc.id) {
        if (_action === ACTIONS.LAST) {
          let name = bc.name || `Location #${bc.id}`;
          if (!bc.name) {
            const li = await _fetchLocationInfo(bc.id);
            if (scanFlow !== _flowId || _state !== STATES.AWAITING_LOCATION) return;
            if (li) name = li.name;
          }
          _setLastLocation({ id: bc.id, name: name });
          _showResult(STATES.SUCCESS, `✓ Default location set: ${name}`, 4000);
          return;
        }
        // CREATESTOCK: location override
        _pendingLocationId = bc.id;
        _enterCreateStockQty();
        return;
      }

      // v1.2.6 FASTADD: free token in the location step → commit with SYS:LAST
      if (_action === ACTIONS.FASTADD && bc.type === 'free' && /^STOCK_/i.test(bc.token)) {
        const last = _getLastLocation();
        if (last && last.id) {
          _pendingLocationId = last.id;
          await _execCreateStockCommit(bc.token);
          return;
        }
        _setState(STATES.AWAITING_LOCATION,
          '⚠ No default location stored — scan a Location label first');
        return;
      }

      // v1.2.6 FASTADD: bound stock item in the location step → merge if same part
      if (_action === ACTIONS.FASTADD && bc.type === 'stockitem') {
        let bcPartId = bc.partId;
        let bcLocId = bc.locationId;
        if (bcPartId == null || bcLocId == null) {
          const bi = await _fetchStockItemInfo(bc.id);
          if (scanFlow !== _flowId || _state !== STATES.AWAITING_LOCATION) return;
          if (bi) {
            bcPartId = bi.part_id;
            bcLocId = bi.location_id;
          }
        }
        if (bcPartId === _pendingPartId &&
            (_pendingLocationId == null || bcLocId === _pendingLocationId)) {
          await _execFastAddMerge(bc);
          return;
        }
        _setState(STATES.AWAITING_LOCATION,
          `⚠ ${bc.token || 'Token'} belongs to a different part.\n→ Scan a Location label or a FREE STOCK_XXXX token`);
        return;
      }

      // CREATESTOCK/FASTADD: SYS:QTY:nnn = keep the SYS:LAST location and set qty
      if (_action === ACTIONS.CREATESTOCK || _action === ACTIONS.FASTADD) {
        const qty = _parseQuantityCode(raw);
        if (qty !== null && qty > 0) {
          const last = _getLastLocation();
          if (last && last.id) {
            _pendingLocationId = last.id;
            _pendingQty = qty;
            _setState(STATES.AWAITING_TOKEN,
              `Quantity: ${_pendingQty}\n Scan a FREE STOCK_XXXX token to bind the new instance`);
            return;
          }
          _setState(STATES.AWAITING_LOCATION,
            '? No default location stored - scan a Location label');
          return;
        }
      }

      _setState(STATES.AWAITING_LOCATION,
        `⚠ Not a Location label!\n${_action === ACTIONS.LAST
          ? '→ Scan a Location label to set the default'
          : _locationPrompt('Create stock')}`);
      return;
    }

    // ── STATE: AWAITING_QTY ──────────────────────────────────────────────
    if (_state === STATES.AWAITING_QTY) {
      if (ctrlCode) {
        _armAction(ctrlCode);
        return;
      }

      const qty = _parseQuantityCode(raw);
      if (qty !== null && qty > 0) {
        _pendingQty = qty;
        _setState(STATES.AWAITING_TOKEN,
          `Quantity: ${_pendingQty}\n→ Scan a FREE STOCK_XXXX token to bind the new instance`);
        return;
      }

      const bc = await _parseInvenTreeBarcode(raw);

      // Scanning the free token here accepts the current (default) quantity
      if (scanFlow !== _flowId || _state !== STATES.AWAITING_QTY) return;
      if (bc.type === 'free' && /^STOCK_/i.test(bc.token)) {
        if (_pendingQty == null || _pendingQty <= 0) {
          _setState(STATES.AWAITING_QTY,
            '⚠ Quantity required — scan SYS:QTY:nnn first');
          return;
        }
        await _execCreateStockCommit(bc.token);
        return;
      }

      _setState(STATES.AWAITING_QTY,
        '⚠ Scan a Quantity Code (SYS:QTY:nnn) or a FREE STOCK_ token');
      return;
    }

    // ── STATE: AWAITING_TOKEN ────────────────────────────────────────────
    if (_state === STATES.AWAITING_TOKEN) {
      if (ctrlCode) {
        _armAction(ctrlCode);
        return;
      }

      const bc = await _parseInvenTreeBarcode(raw);

      if (scanFlow !== _flowId || _state !== STATES.AWAITING_TOKEN) return;
      if (bc.type === 'free' && /^STOCK_/i.test(bc.token)) {
        await _execCreateStockCommit(bc.token);
        return;
      }
      if (bc.type === 'stockitem') {
        if (_action === ACTIONS.FASTADD) {
          // Merge: same part + same location → add the reel qty to the existing instance
          let bcPartId = bc.partId;
          let bcLocId = bc.locationId;
          if (bcPartId == null || bcLocId == null) {
            const bi = await _fetchStockItemInfo(bc.id);
            if (scanFlow !== _flowId || _state !== STATES.AWAITING_TOKEN) return;
            if (bi) {
              bcPartId = bi.part_id;
              bcLocId = bi.location_id;
            }
          }
          if (bcPartId === _pendingPartId && bcLocId === _pendingLocationId) {
            await _execFastAddMerge(bc);
            return;
          }
          _setState(STATES.AWAITING_TOKEN,
            `⚠ ${bc.token || 'Token'} belongs to a different part or location.\n→ Scan a FREE STOCK_XXXX token for a new instance`);
          return;
        }
        _setState(STATES.AWAITING_TOKEN,
          `⚠ ${bc.token || 'Token'} already bound to: ${bc.name || 'a stock item'}\n→ Scan a FREE STOCK_XXXX token`);
        return;
      }
      if (bc.type === 'stocklocation' || bc.type === 'part') {
        _setState(STATES.AWAITING_TOKEN,
          `⚠ That is a ${bc.type} label, not a free STOCK_ token\n→ Scan a FREE STOCK_XXXX token`);
        return;
      }
      _setState(STATES.AWAITING_TOKEN,
        '⚠ Scan a FREE STOCK_XXXX token label');
      return;
    }

    // ── STATE: AWAITING_CONFIRM (double-scan delete gate) ────────────────
    if (_state === STATES.AWAITING_CONFIRM) {
      if (ctrlCode) {
        _clearPendingDelete();
        _action = ctrlCode;
        _stockItemId = null;
        _itemInfo = null;
        _setState(STATES.AWAITING_ITEM,
          `Action: ${ctrlCode} — ${_itemPrompt(ctrlCode)}`);
        return;
      }

      if (_pendingDeleteKind === 'part') {
        const lk = await _lookupDistributorBarcode(raw);
        if (lk && lk.found && lk.part_id === _pendingDeleteId) {
          await _execDeletePartCommit();
        } else {
          _setState(STATES.AWAITING_CONFIRM,
            `⚠ Different label — scan the SAME label again to confirm\n⛔ DELETE PART: "${_pendingDeleteDesc}"`);
        }
        return;
      }

      if (_pendingDeleteKind === 'stock') {
        const bc = await _parseInvenTreeBarcode(raw);
        if (bc.type === 'stockitem' && bc.id === _pendingDeleteId) {
          await _execDeleteStockCommit(bc.token || _pendingToken);
        } else {
          _setState(STATES.AWAITING_CONFIRM,
            `⚠ Different label — scan the SAME token again to confirm\n⛔ DELETE STOCK: "${_pendingDeleteDesc}"`);
        }
        return;
      }

      _resetToIdle();
      return;
    }
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  Scanner wedge listener
  // ═══════════════════════════════════════════════════════════════════════════

  const MAX_INTER_KEY_MS = 50;
  const MIN_SCAN_LENGTH  = 3;
  const IDLE_RESET_MS    = 600;

  let _buffer   = '';
  let _times    = [];
  let _lastTime = 0;

  function _onKey(e) {
    const now   = Date.now();
    const delta = now - _lastTime;
    _lastTime   = now;

    if (e.key === 'Enter') {
      const captured      = _buffer;
      const capturedTimes = _times.slice();
      _buffer = '';
      _times  = [];

      if (captured.length < MIN_SCAN_LENGTH) return;

      let fastGaps = 0;
      for (let i = 1; i < capturedTimes.length; i++) {
        if (capturedTimes[i] - capturedTimes[i - 1] < MAX_INTER_KEY_MS) fastGaps++;
      }
      const totalGaps = Math.max(capturedTimes.length - 1, 1);
      const fastRatio = fastGaps / totalGaps;

      if (fastRatio >= 0.75) {
        e.preventDefault();
        e.stopPropagation();

        const ae = document.activeElement;
        if (ae && (ae.tagName === 'INPUT' || ae.tagName === 'TEXTAREA')) {
          ae.value = '';
        }
        handleScan(captured);
      }
      return;
    }

    if (e.key.length > 1) return;

    if (delta > IDLE_RESET_MS && _buffer.length > 0) {
      _buffer = '';
      _times  = [];
    }

    _buffer += e.key;
    _times.push(now);
  }

  // ═══════════════════════════════════════════════════════════════════════════
  //  Public API
  // ═══════════════════════════════════════════════════════════════════════════

  function init(onStateChange) {
    _onStateChange = onStateChange;
    _loadLog();   // Restore from localStorage
    document.addEventListener('keydown', _onKey, { capture: true });
    _resetToIdle();
  }

  function destroy() {
    document.removeEventListener('keydown', _onKey, { capture: true });
    _onStateChange = null;
    _flowId++;
    clearTimeout(_resultTimer);
    clearTimeout(_comboTimer);
    _stopBurst();
    _buffer = '';
    _times  = [];
  }

  function getActionLog() {
    return _actionLog;
  }

  global.PureScan = {
    init,
    destroy,
    handleScan,
    getActionLog,
    clearLog,
    STATES,
    ACTIONS,
    CONTROL_CODES,
  };

})(window);

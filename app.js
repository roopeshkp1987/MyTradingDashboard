/**
 * NSE Market Dashboard — app.js
 * ================================
 * Handles:
 *  - Loading stocks.json data
 *  - 3 filter modes with client-side filtering
 *  - Sortable stock table
 *  - TradingView Lightweight Charts (price + EMA lines + volume + vol MA)
 *  - Live chart data via /api/chart proxy
 *  - Refresh modal with live log streaming
 */

(function () {
  'use strict';

  /* ── State ─────────────────────────────────────────────────────────── */
  let allStocks      = [];          // Full loaded universe
  let filteredStocks = [];          // Currently displayed subset
  let activeSymbol   = null;        // Symbol shown in chart
  let activeRow      = null;        // Active TR element
  let sortCol        = 'change_pct';
  let sortDir        = -1;          // -1 = desc, 1 = asc
  let currentRange   = '1y';
  let currentInterval = '1d';

  // Chart instances
  let priceChart     = null;
  let volumeChart    = null;
  let candleSeries   = null;
  let ema10Series    = null;
  let ema20Series    = null;
  let ema50Series    = null;
  let ema200Series   = null;
  let volSeries      = null;
  let volMaSeries    = null;

  // Refresh polling
  let refreshPollTimer = null;

  // Drawing tools state
  let activeTool   = 'pointer';  // pointer | trendline | hline | vline | rectangle | fibonacci
  let pendingPoint = null;        // first click anchor for 2-point tools
  let allDrawings  = [];          // array of attached primitives
  let previewPrim  = null;        // live-preview primitive (always attached)

  /* ── DOM refs ──────────────────────────────────────────────────────── */
  const $ = id => document.getElementById(id);
  const filterSelect     = $('filter-select');
  const filterDesc       = $('filter-desc');
  const stockCountEl     = $('stock-count');
  const stockTbody       = $('stock-tbody');
  const stockEmpty       = $('stock-empty');
  const lastUpdatedText  = $('last-updated-text');
  const chartLoading     = $('chart-loading');
  const chartPlaceholder = $('chart-placeholder');
  const chartSymName     = $('chart-sym-name');
  const chartPrice       = $('chart-price');
  const chartChange      = $('chart-change');
  const refreshBtn       = $('refresh-btn');
  const refreshModal     = $('refresh-modal');
  const refreshModalMsg  = $('refresh-modal-msg');
  const refreshLog       = $('refresh-log');
  const refreshModalClose = $('refresh-modal-close');
  const toast            = $('toast');
  const toastMsg         = $('toast-msg');
  const toastIcon        = $('toast-icon');

  /* ═══════════════════════════════════════════════════════════════════
     FILTER DEFINITIONS
  ═══════════════════════════════════════════════════════════════════ */
  const FILTERS = {
    uptrend: {
      label: 'Price > ₹30 · EMA(20) > EMA(50) > EMA(200) · MCap > ₹800 Cr',
      fn: s =>
        s.price        > 30 &&
        s.ema20        !== null && s.ema50 !== null && s.ema200 !== null &&
        s.ema20        > s.ema50 &&
        s.ema50        > s.ema200 &&
        (s.marketcap_cr === null || s.marketcap_cr > 800),
    },
    perf3m: {
      label: 'Price > ₹30 · MCap > ₹800 Cr · 3M Return > 30%',
      fn: s =>
        s.price        > 30 &&
        s.perf_3m      !== null && s.perf_3m > 30 &&
        (s.marketcap_cr === null || s.marketcap_cr > 800),
    },
    volspike: {
      label: 'Price > ₹30 · Daily Change > 3% · Relative Volume > 3×',
      fn: s =>
        s.price            > 30 &&
        s.change_pct       > 3 &&
        s.relative_volume  !== null && s.relative_volume > 3,
    },
  };

  /* ═══════════════════════════════════════════════════════════════════
     DATA LOADING
  ═══════════════════════════════════════════════════════════════════ */
  function loadStocks() {
    fetch('data/stocks.json')
      .then(r => {
        if (!r.ok) throw new Error('stocks.json not found');
        return r.json();
      })
      .then(data => {
        allStocks = data.stocks || [];

        // Format last updated
        if (data.last_updated) {
          const d = new Date(data.last_updated);
          lastUpdatedText.textContent =
            `Updated: ${d.toLocaleDateString('en-IN', { day:'2-digit', month:'short' })} ` +
            `${d.toLocaleTimeString('en-IN', { hour:'2-digit', minute:'2-digit' })}  ·  ` +
            `${data.total} stocks`;
        } else {
          lastUpdatedText.textContent = `${allStocks.length} stocks loaded`;
        }

        applyFilter();
        showToast('✅', `Loaded ${allStocks.length} stocks`, 'success');
      })
      .catch(err => {
        stockTbody.innerHTML = `
          <tr><td colspan="4" style="padding:40px 20px; text-align:center; color:var(--text-muted);">
            <div style="font-size:32px; margin-bottom:12px;">⚠️</div>
            <div style="font-size:14px; font-weight:600; color:var(--text-secondary); margin-bottom:8px;">
              No Data Found
            </div>
            <div style="font-size:12px; line-height:1.7;">
              Click <strong>Refresh Market Data</strong> to fetch live data,<br/>
              or run <code style="background:rgba(255,255,255,0.08); padding:2px 6px; border-radius:4px;">
              python scripts/fetch_data.py</code> first.
            </div>
          </td></tr>`;
        lastUpdatedText.textContent = 'No data — click Refresh';
        stockCountEl.textContent = '0';
        console.warn('Could not load stocks.json:', err);
      });
  }

  /* ═══════════════════════════════════════════════════════════════════
     FILTERING
  ═══════════════════════════════════════════════════════════════════ */
  function applyFilter() {
    const filterKey = filterSelect.value;
    const filter    = FILTERS[filterKey];
    filterDesc.textContent = filter.label;

    filteredStocks = allStocks.filter(filter.fn);

    // Sort by current sort column
    sortStocks();
    renderTable();
  }

  function sortStocks() {
    filteredStocks.sort((a, b) => {
      let av = a[sortCol], bv = b[sortCol];
      if (av === null || av === undefined) av = sortDir > 0 ? Infinity : -Infinity;
      if (bv === null || bv === undefined) bv = sortDir > 0 ? Infinity : -Infinity;
      if (typeof av === 'string') return av.localeCompare(bv) * sortDir;
      return (av - bv) * sortDir;
    });
  }

  /* ═══════════════════════════════════════════════════════════════════
     TABLE RENDERING
  ═══════════════════════════════════════════════════════════════════ */
  function fmtPrice(p)  { return p != null ? `₹${Number(p).toLocaleString('en-IN', {minimumFractionDigits:2, maximumFractionDigits:2})}` : '—'; }
  function fmtChange(c) { return c != null ? `${c >= 0 ? '+' : ''}${Number(c).toFixed(2)}%` : '—'; }
  function fmtMC(mc) {
    if (mc == null) return '—';
    if (mc >= 100000) return `${(mc / 100000).toFixed(1)}L`;   // Lakh crore
    if (mc >= 1000)   return `${(mc / 1000).toFixed(1)}K`;     // Thousand crore
    return `${Math.round(mc)}`;
  }

  function renderTable() {
    // Update count
    stockCountEl.textContent = filteredStocks.length;

    if (filteredStocks.length === 0) {
      stockTbody.innerHTML = '';
      stockEmpty.style.display = 'flex';
      return;
    }
    stockEmpty.style.display = 'none';

    // Update sort headers
    document.querySelectorAll('#stock-table th').forEach(th => {
      th.classList.remove('sorted');
      th.removeAttribute('data-arrow');
    });
    const sortedTh = document.querySelector(`th[data-col="${sortCol}"]`);
    if (sortedTh) {
      sortedTh.classList.add('sorted');
      sortedTh.dataset.arrow = sortDir < 0 ? '↓' : '↑';
    }

    // Build rows
    const rows = filteredStocks.map((s, idx) => {
      const chgClass = s.change_pct > 0 ? 'up' : (s.change_pct < 0 ? 'down' : 'flat');
      const arrow    = s.change_pct > 0 ? '▲' : (s.change_pct < 0 ? '▼' : '—');
      return `
        <tr class="stock-row" data-symbol="${escHtml(s.symbol)}" tabindex="0"
            role="button" aria-label="View chart for ${escHtml(s.symbol)}">
          <td>
            <div class="sym-name">${escHtml(s.symbol)}</div>
          </td>
          <td class="price-cell">${fmtPrice(s.price)}</td>
          <td>
            <div class="chg-pill ${chgClass}">${arrow} ${fmtChange(s.change_pct)}</div>
          </td>
          <td class="mc-cell">${fmtMC(s.marketcap_cr)}</td>
        </tr>`;
    }).join('');

    stockTbody.innerHTML = rows;

    // Restore active row highlight
    if (activeSymbol) {
      const newActive = stockTbody.querySelector(`[data-symbol="${activeSymbol}"]`);
      if (newActive) {
        newActive.classList.add('active');
        activeRow = newActive;
      }
    }

    // Attach click + keyboard events via delegation
    stockTbody.onclick = null;
    stockTbody.addEventListener('click', handleRowClick);
    stockTbody.addEventListener('keydown', handleRowKeydown);
  }

  function handleRowClick(e) {
    const row = e.target.closest('.stock-row');
    if (!row) return;
    selectRow(row);
  }

  function handleRowKeydown(e) {
    if (e.key === 'Enter' || e.key === ' ') {
      const row = e.target.closest('.stock-row');
      if (row) { e.preventDefault(); selectRow(row); }
    }
    if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
      e.preventDefault();
      const rows = [...stockTbody.querySelectorAll('.stock-row')];
      const cur  = rows.indexOf(document.activeElement.closest('.stock-row'));
      const next = e.key === 'ArrowDown' ? cur + 1 : cur - 1;
      if (rows[next]) { rows[next].focus(); selectRow(rows[next]); }
    }
  }

  function selectRow(row) {
    if (activeRow) activeRow.classList.remove('active');
    row.classList.add('active');
    activeRow  = row;
    activeSymbol = row.dataset.symbol;
    row.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
    loadChart(activeSymbol, currentRange, currentInterval);
  }

  /* ═══════════════════════════════════════════════════════════════════
     SORT (column header click)
  ═══════════════════════════════════════════════════════════════════ */
  document.querySelectorAll('#stock-table th[data-col]').forEach(th => {
    th.addEventListener('click', () => {
      const col = th.dataset.col;
      if (sortCol === col) {
        sortDir *= -1;
      } else {
        sortCol = col;
        sortDir = col === 'symbol' ? 1 : -1;
      }
      sortStocks();
      renderTable();
    });
  });

  /* ═══════════════════════════════════════════════════════════════════
     CHART INITIALIZATION (TradingView Lightweight Charts v5)
  ═══════════════════════════════════════════════════════════════════ */
  const CHART_OPTS = {
    layout: {
      background:   { type: 'solid', color: '#050c1a' },
      textColor:    '#94a3b8',
      fontFamily:   "'JetBrains Mono', 'Inter', monospace",
      fontSize:      11,
    },
    grid: {
      vertLines:  { color: 'rgba(255,255,255,0.04)' },
      horzLines:  { color: 'rgba(255,255,255,0.04)' },
    },
    crosshair: {
      mode: 1,   // Normal mode
      vertLine: { color: 'rgba(59,130,246,0.5)', labelBackgroundColor: '#0f1f35' },
      horzLine: { color: 'rgba(59,130,246,0.5)', labelBackgroundColor: '#0f1f35' },
    },
    rightPriceScale: {
      borderColor: 'rgba(255,255,255,0.07)',
      scaleMargins: { top: 0.1, bottom: 0.05 },
    },
    timeScale: {
      borderColor:    'rgba(255,255,255,0.07)',
      timeVisible:    true,
      secondsVisible: false,
      rightOffset:    5,
    },
    handleScroll: true,
    handleScale:  true,
  };

  function initCharts() {
    const priceEl  = $('price-chart');
    const volumeEl = $('volume-chart');

    // Destroy old instances if any
    if (priceChart)  { priceChart.remove();  priceChart  = null; }
    if (volumeChart) { volumeChart.remove(); volumeChart = null; }

    // Price chart
    priceChart = LightweightCharts.createChart(priceEl, {
      ...CHART_OPTS,
      width:  priceEl.clientWidth,
      height: priceEl.clientHeight,
    });

    // Candlestick series
    candleSeries = priceChart.addSeries(LightweightCharts.CandlestickSeries, {
      upColor:          '#10b981',
      downColor:        '#ef4444',
      borderUpColor:    '#10b981',
      borderDownColor:  '#ef4444',
      wickUpColor:      '#10b981',
      wickDownColor:    '#ef4444',
    });

    // EMA 10 – orange
    ema10Series = priceChart.addSeries(LightweightCharts.LineSeries, {
      color:       '#f97316',
      lineWidth:   1,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    // EMA 20 – cyan
    ema20Series = priceChart.addSeries(LightweightCharts.LineSeries, {
      color:       '#22d3ee',
      lineWidth:   1.5,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    // EMA 50 – purple
    ema50Series = priceChart.addSeries(LightweightCharts.LineSeries, {
      color:       '#a78bfa',
      lineWidth:   2,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    // EMA 200 – amber
    ema200Series = priceChart.addSeries(LightweightCharts.LineSeries, {
      color:       '#f59e0b',
      lineWidth:   2.5,
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    // Volume chart (separate)
    volumeChart = LightweightCharts.createChart(volumeEl, {
      ...CHART_OPTS,
      width:  volumeEl.clientWidth,
      height: volumeEl.clientHeight,
      rightPriceScale: {
        borderColor: 'rgba(255,255,255,0.07)',
        scaleMargins: { top: 0.1, bottom: 0 },
      },
    });

    // Volume histogram
    volSeries = volumeChart.addSeries(LightweightCharts.HistogramSeries, {
      color:          '#1e3a5f',
      priceFormat:    { type: 'volume' },
      priceScaleId:   'right',
    });

    // Volume MA-50 line
    volMaSeries = volumeChart.addSeries(LightweightCharts.LineSeries, {
      color:       '#38bdf8',
      lineWidth:   2,
      priceScaleId: 'right',
      priceLineVisible: false,
      lastValueVisible: false,
      crosshairMarkerVisible: false,
    });

    // Sync crosshair between price and volume charts + drawing preview
    priceChart.subscribeCrosshairMove(param => {
      if (param.time) {
        volumeChart.setCrosshairPosition(
          param.seriesData.get(candleSeries)?.close ?? NaN,
          param.time,
          volSeries
        );
      } else {
        volumeChart.clearCrosshairPosition();
      }
      // Update live preview when drawing a 2-point shape
      if (activeTool !== 'pointer' && pendingPoint && param.point && param.time && previewPrim) {
        const price = candleSeries.coordinateToPrice(param.point.y);
        if (price !== null) {
          previewPrim.set(activeTool, pendingPoint, { time: param.time, price });
        }
      }
    });

    // Drawing click handler
    priceChart.subscribeClick(param => {
      if (activeTool === 'pointer' || !param.point || !param.time) return;
      const price = candleSeries.coordinateToPrice(param.point.y);
      if (price === null) return;
      const pt = { time: param.time, price };

      if (activeTool === 'hline') {
        addDrawing(new HLinePrim(price));
        showToast('─', 'Horizontal line placed', 'success');
        return;
      }
      if (activeTool === 'vline') {
        addDrawing(new VLinePrim(param.time));
        showToast('│', 'Vertical line placed', 'success');
        return;
      }
      // Two-point tools
      if (!pendingPoint) {
        pendingPoint = pt;
        if (previewPrim) previewPrim.set(activeTool, pt, pt);
        $('chart-body').classList.add('pending-point');
        document.querySelector('.draw-btn.active')?.classList.add('pulsing');
      } else {
        const p1 = pendingPoint;
        pendingPoint = null;
        if (previewPrim) previewPrim.clear();
        $('chart-body').classList.remove('pending-point');
        document.querySelector('.draw-btn.active')?.classList.remove('pulsing');
        if (activeTool === 'trendline')  { addDrawing(new TrendPrim(p1, pt));  showToast('↗', 'Trend line drawn', 'success'); }
        if (activeTool === 'rectangle')  { addDrawing(new RectPrim(p1, pt));   showToast('▭', 'Rectangle drawn', 'success'); }
        if (activeTool === 'fibonacci')  { addDrawing(new FibPrim(p1, pt));    showToast('≋', 'Fibonacci drawn', 'success'); }
      }
    });

    // Sync time scale scroll/zoom
    let isSyncing = false;
    priceChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (isSyncing) return;
      isSyncing = true;
      volumeChart.timeScale().setVisibleLogicalRange(range);
      isSyncing = false;
    });
    volumeChart.timeScale().subscribeVisibleLogicalRangeChange(range => {
      if (isSyncing) return;
      isSyncing = true;
      priceChart.timeScale().setVisibleLogicalRange(range);
      isSyncing = false;
    });

    // Resize observer
    const ro = new ResizeObserver(() => {
      if (priceChart)  priceChart.resize(priceEl.clientWidth,  priceEl.clientHeight);
      if (volumeChart) volumeChart.resize(volumeEl.clientWidth, volumeEl.clientHeight);
    });
    ro.observe(priceEl);
    ro.observe(volumeEl);

    // Attach drawing preview primitive
    previewPrim = new PreviewPrim();
    candleSeries.attachPrimitive(previewPrim);
  }

  /* ═══════════════════════════════════════════════════════════════════
     EMA calculation (client-side)
  ═══════════════════════════════════════════════════════════════════ */
  function calcEMA(closes, span) {
    const k = 2 / (span + 1);
    const result = [];
    let ema = null;
    for (const c of closes) {
      if (ema === null) { ema = c; }
      else              { ema = c * k + ema * (1 - k); }
      result.push(ema);
    }
    return result;
  }

  function calcSMA(values, window) {
    return values.map((_, i) => {
      if (i < window - 1) return null;
      const slice = values.slice(i - window + 1, i + 1);
      return slice.reduce((a, b) => a + b, 0) / window;
    });
  }

  /* ═══════════════════════════════════════════════════════════════════
     DRAWING TOOL PRIMITIVES  (Lightweight Charts v5 primitive API)
  ═══════════════════════════════════════════════════════════════════ */

  // ── Shared base ─────────────────────────────────────────────────────
  class DrawBase {
    constructor() { this._chart = null; this._series = null; this._req = null; }
    attached({ chart, series, requestUpdate }) {
      this._chart = chart; this._series = series; this._req = requestUpdate;
    }
    detached() { this._chart = null; this._series = null; this._req = null; }
    updateAllViews() {}
    _refresh() { if (this._req) this._req(); }
    _makeView(drawFn, zOrder = 'normal') {
      const renderer = { draw: t => drawFn(t) };
      return { renderer: () => renderer, zOrder: () => zOrder };
    }
  }

  // ── Horizontal line ─────────────────────────────────────────────────
  class HLinePrim extends DrawBase {
    constructor(price) {
      super(); this._price = price;
      this._views = [this._makeView(t => this._draw(t))];
    }
    paneViews() { return this._views; }
    _draw(target) {
      if (!this._series) return;
      target.useMediaCoordinateSpace(({ context: c, mediaSize: ms }) => {
        const y = this._series.priceToCoordinate(this._price);
        if (y === null) return;
        c.save();
        c.strokeStyle = '#3b82f6'; c.lineWidth = 1.5; c.setLineDash([9, 4]);
        c.beginPath(); c.moveTo(0, y); c.lineTo(ms.width, y); c.stroke();
        c.setLineDash([]);
        const lbl = `\u20b9${this._price.toFixed(2)}`;
        c.font = '10px JetBrains Mono, monospace';
        const tw = c.measureText(lbl).width;
        c.fillStyle = 'rgba(10,22,40,0.88)';
        c.fillRect(ms.width - tw - 14, y - 9, tw + 10, 16);
        c.strokeStyle = 'rgba(59,130,246,0.4)'; c.lineWidth = 1;
        c.strokeRect(ms.width - tw - 14, y - 9, tw + 10, 16);
        c.fillStyle = '#3b82f6'; c.fillText(lbl, ms.width - tw - 9, y + 4);
        c.restore();
      });
    }
  }

  // ── Vertical line ────────────────────────────────────────────────────
  class VLinePrim extends DrawBase {
    constructor(time) {
      super(); this._time = time;
      this._views = [this._makeView(t => this._draw(t))];
    }
    paneViews() { return this._views; }
    _draw(target) {
      if (!this._chart) return;
      target.useMediaCoordinateSpace(({ context: c, mediaSize: ms }) => {
        const x = this._chart.timeScale().timeToCoordinate(this._time);
        if (x === null) return;
        c.save();
        c.strokeStyle = '#8b5cf6'; c.lineWidth = 1.5; c.setLineDash([9, 4]);
        c.beginPath(); c.moveTo(x, 0); c.lineTo(x, ms.height); c.stroke();
        c.restore();
      });
    }
  }

  // ── Trend line (extends to chart edges) ──────────────────────────────
  class TrendPrim extends DrawBase {
    constructor(p1, p2) {
      super(); this._p1 = p1; this._p2 = p2;
      this._views = [this._makeView(t => this._draw(t))];
    }
    paneViews() { return this._views; }
    _draw(target) {
      if (!this._chart || !this._series) return;
      target.useMediaCoordinateSpace(({ context: c, mediaSize: ms }) => {
        const ts = this._chart.timeScale();
        const x1 = ts.timeToCoordinate(this._p1.time);
        const y1 = this._series.priceToCoordinate(this._p1.price);
        const x2 = ts.timeToCoordinate(this._p2.time);
        const y2 = this._series.priceToCoordinate(this._p2.price);
        if (x1 === null || y1 === null || x2 === null || y2 === null) return;
        if (Math.abs(x2 - x1) < 1) return;
        const slope = (y2 - y1) / (x2 - x1);
        c.save();
        c.strokeStyle = '#f59e0b'; c.lineWidth = 1.5;
        c.beginPath();
        c.moveTo(0, y1 + slope * (0 - x1));
        c.lineTo(ms.width, y1 + slope * (ms.width - x1));
        c.stroke();
        c.fillStyle = '#f59e0b';
        [[x1, y1], [x2, y2]].forEach(([px, py]) => {
          c.beginPath(); c.arc(px, py, 3.5, 0, Math.PI * 2); c.fill();
        });
        c.restore();
      });
    }
  }

  // ── Rectangle ────────────────────────────────────────────────────────
  class RectPrim extends DrawBase {
    constructor(p1, p2) {
      super(); this._p1 = p1; this._p2 = p2;
      this._views = [this._makeView(t => this._draw(t))];
    }
    paneViews() { return this._views; }
    _draw(target) {
      if (!this._chart || !this._series) return;
      target.useMediaCoordinateSpace(({ context: c }) => {
        const ts = this._chart.timeScale();
        const x1 = ts.timeToCoordinate(this._p1.time);
        const y1 = this._series.priceToCoordinate(this._p1.price);
        const x2 = ts.timeToCoordinate(this._p2.time);
        const y2 = this._series.priceToCoordinate(this._p2.price);
        if (x1 === null || y1 === null || x2 === null || y2 === null) return;
        c.save();
        c.fillStyle   = 'rgba(34,211,238,0.07)';
        c.strokeStyle = '#22d3ee'; c.lineWidth = 1.5;
        c.fillRect(x1, y1, x2 - x1, y2 - y1);
        c.strokeRect(x1, y1, x2 - x1, y2 - y1);
        c.restore();
      });
    }
  }

  // ── Fibonacci retracement ─────────────────────────────────────────────
  const FIB_LEVELS = [
    { r: 0,     color: '#ef4444', lbl: '0%'    },
    { r: 0.236, color: '#f97316', lbl: '23.6%' },
    { r: 0.382, color: '#f59e0b', lbl: '38.2%' },
    { r: 0.5,   color: '#10b981', lbl: '50%'   },
    { r: 0.618, color: '#3b82f6', lbl: '61.8%' },
    { r: 0.786, color: '#8b5cf6', lbl: '78.6%' },
    { r: 1.0,   color: '#ef4444', lbl: '100%'  },
  ];

  class FibPrim extends DrawBase {
    constructor(p1, p2) {
      super(); this._p1 = p1; this._p2 = p2;
      this._views = [this._makeView(t => this._draw(t))];
    }
    paneViews() { return this._views; }
    _draw(target) {
      if (!this._chart || !this._series) return;
      target.useMediaCoordinateSpace(({ context: c, mediaSize: ms }) => {
        const ts   = this._chart.timeScale();
        const x1   = ts.timeToCoordinate(this._p1.time);
        const x2   = ts.timeToCoordinate(this._p2.time);
        if (x1 === null || x2 === null) return;
        const xL   = Math.min(x1, x2);
        const high = Math.max(this._p1.price, this._p2.price);
        const low  = Math.min(this._p1.price, this._p2.price);
        c.save();
        c.font = '10px JetBrains Mono, monospace';
        let prevY = null;
        FIB_LEVELS.forEach(({ r, color, lbl }) => {
          const price = high - r * (high - low);
          const y = this._series.priceToCoordinate(price);
          if (y === null) return;
          // Band fill
          if (prevY !== null) {
            c.fillStyle = color + '14';
            c.fillRect(xL, Math.min(y, prevY), ms.width - xL, Math.abs(y - prevY));
          }
          prevY = y;
          // Line
          c.globalAlpha = 0.75; c.strokeStyle = color;
          c.lineWidth = 1; c.setLineDash([7, 4]);
          c.beginPath(); c.moveTo(xL, y); c.lineTo(ms.width, y); c.stroke();
          // Label
          c.setLineDash([]); c.globalAlpha = 1;
          c.fillStyle = color;
          c.fillText(`${lbl}  \u20b9${price.toFixed(1)}`, xL + 6, y - 3);
        });
        c.restore();
      });
    }
  }

  // ── Preview primitive (live ghost while drawing) ─────────────────────
  class PreviewPrim extends DrawBase {
    constructor() {
      super();
      this._tool = null; this._p1 = null; this._p2 = null;
      this._renderer = { draw: t => this._draw(t) };
      this._view = { renderer: () => this._renderer, zOrder: () => 'top' };
      this._views = [this._view]; this._empty = [];
    }
    paneViews() {
      return (this._tool && this._p1 && this._p2) ? this._views : this._empty;
    }
    set(tool, p1, p2) { this._tool = tool; this._p1 = p1; this._p2 = p2; this._refresh(); }
    clear()           { this._tool = null; this._p1 = null; this._p2 = null; this._refresh(); }
    _draw(target) {
      if (!this._chart || !this._series || !this._p1 || !this._p2) return;
      target.useMediaCoordinateSpace(({ context: c, mediaSize: ms }) => {
        const ts  = this._chart.timeScale();
        const ser = this._series;
        c.save(); c.globalAlpha = 0.6; c.setLineDash([5, 4]);
        if (this._tool === 'trendline') {
          const x1 = ts.timeToCoordinate(this._p1.time), y1 = ser.priceToCoordinate(this._p1.price);
          const x2 = ts.timeToCoordinate(this._p2.time), y2 = ser.priceToCoordinate(this._p2.price);
          if (x1 !== null && y1 !== null && x2 !== null && y2 !== null && Math.abs(x2 - x1) > 1) {
            const slope = (y2 - y1) / (x2 - x1);
            c.strokeStyle = '#f59e0b'; c.lineWidth = 1.5;
            c.beginPath();
            c.moveTo(0, y1 + slope * -x1); c.lineTo(ms.width, y1 + slope * (ms.width - x1));
            c.stroke();
            c.setLineDash([]); c.fillStyle = '#f59e0b';
            c.beginPath(); c.arc(x1, y1, 4, 0, Math.PI * 2); c.fill();
          }
        } else if (this._tool === 'rectangle') {
          const x1 = ts.timeToCoordinate(this._p1.time), y1 = ser.priceToCoordinate(this._p1.price);
          const x2 = ts.timeToCoordinate(this._p2.time), y2 = ser.priceToCoordinate(this._p2.price);
          if (x1 !== null && y1 !== null && x2 !== null && y2 !== null) {
            c.strokeStyle = '#22d3ee'; c.lineWidth = 1.5;
            c.fillStyle = 'rgba(34,211,238,0.05)';
            c.fillRect(x1, y1, x2 - x1, y2 - y1);
            c.strokeRect(x1, y1, x2 - x1, y2 - y1);
            c.setLineDash([]); c.fillStyle = '#22d3ee';
            c.beginPath(); c.arc(x1, y1, 4, 0, Math.PI * 2); c.fill();
          }
        } else if (this._tool === 'fibonacci') {
          const x1 = ts.timeToCoordinate(this._p1.time);
          const x2 = ts.timeToCoordinate(this._p2.time);
          if (x1 !== null && x2 !== null) {
            const xL = Math.min(x1, x2);
            const high = Math.max(this._p1.price, this._p2.price);
            const low  = Math.min(this._p1.price, this._p2.price);
            FIB_LEVELS.forEach(({ r, color }) => {
              const y = ser.priceToCoordinate(high - r * (high - low));
              if (y === null) return;
              c.strokeStyle = color; c.lineWidth = 1;
              c.beginPath(); c.moveTo(xL, y); c.lineTo(ms.width, y); c.stroke();
            });
          }
        }
        c.restore();
      });
    }
  }

  // ── Drawing manager helpers ──────────────────────────────────────────
  function addDrawing(prim) {
    if (!candleSeries) return;
    allDrawings.push(prim);
    candleSeries.attachPrimitive(prim);
  }

  function undoLastDrawing() {
    if (!candleSeries || allDrawings.length === 0) return;
    const last = allDrawings.pop();
    candleSeries.detachPrimitive(last);
    showToast('↩', 'Drawing removed', 'success');
  }

  function clearAllDrawings() {
    if (!candleSeries) return;
    [...allDrawings].forEach(p => candleSeries.detachPrimitive(p));
    allDrawings.length = 0;
    showToast('🗑', 'All drawings cleared', 'success');
  }

  // ── Tool activation ──────────────────────────────────────────────────
  function setActiveTool(tool) {
    activeTool   = tool;
    pendingPoint = null;
    if (previewPrim) previewPrim.clear();
    const chartBody = $('chart-body');
    if (chartBody) {
      chartBody.classList.remove('pending-point');
      chartBody.classList.toggle('drawing-mode', tool !== 'pointer');
    }
    document.querySelectorAll('.draw-btn').forEach(btn => btn.classList.remove('pulsing'));
    document.querySelectorAll('.draw-btn[data-tool]').forEach(btn =>
      btn.classList.toggle('active', btn.dataset.tool === tool)
    );
  }

  // ── Drawing toolbar event binding ────────────────────────────────────
  function initDrawingToolbar() {
    document.querySelectorAll('.draw-btn[data-tool]').forEach(btn => {
      btn.addEventListener('click', () => setActiveTool(btn.dataset.tool));
    });
    const undoBtn  = $('undo-drawing-btn');
    const clearBtn = $('clear-drawings-btn');
    if (undoBtn)  undoBtn.addEventListener('click', undoLastDrawing);
    if (clearBtn) clearBtn.addEventListener('click', clearAllDrawings);
  }

  /* ═══════════════════════════════════════════════════════════════════
     CHART DATA LOADING
  ═══════════════════════════════════════════════════════════════════ */
  function loadChart(symbol, range, interval) {
    if (!symbol) return;

    // Show loading state
    chartLoading.classList.add('visible');
    chartPlaceholder.style.display = 'none';
    chartSymName.textContent = symbol;
    chartPrice.textContent   = '—';
    chartChange.textContent  = '—';
    chartChange.className    = '';

    // Make sure charts are initialized
    if (!priceChart) initCharts();

    const url = `/api/chart?symbol=${encodeURIComponent(symbol + '.NS')}&range=${range}&interval=${interval}`;

    fetch(url)
      .then(r => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`);
        return r.json();
      })
      .then(data => {
        parseAndRenderChart(data, symbol);
      })
      .catch(err => {
        chartLoading.classList.remove('visible');
        showToast('❌', `Chart error: ${err.message}`, 'error');
        console.error('Chart load error:', err);
      });
  }

  function parseAndRenderChart(data, symbol) {
    try {
      const result = data?.chart?.result?.[0];
      if (!result) throw new Error('No chart data returned from Yahoo Finance');

      const timestamps = result.timestamp;
      const quote      = result.indicators?.quote?.[0];
      const adjClose   = result.indicators?.adjclose?.[0]?.adjclose || quote?.close;

      if (!timestamps || !quote) throw new Error('Malformed chart data');

      // Build OHLCV arrays aligned by timestamp
      const candles = [];
      const volumes = [];
      const closes  = [];

      for (let i = 0; i < timestamps.length; i++) {
        const o = quote.open[i];
        const h = quote.high[i];
        const l = quote.low[i];
        const c = quote.close[i];
        const v = quote.volume[i];
        if (o == null || h == null || l == null || c == null) continue;

        const time = timestamps[i];  // Unix seconds → Lightweight Charts expects YYYY-MM-DD or unix
        candles.push({ time, open: o, high: h, low: l, close: c });
        volumes.push({ time, value: v || 0, color: c >= o ? 'rgba(16,185,129,0.5)' : 'rgba(239,68,68,0.5)' });
        closes.push(c);
      }

      if (candles.length === 0) throw new Error('No valid OHLCV data');

      // Calculate EMAs
      const emaValues10  = calcEMA(closes, 10);
      const emaValues20  = calcEMA(closes, 20);
      const emaValues50  = calcEMA(closes, 50);
      const emaValues200 = calcEMA(closes, 200);

      // Volume MA-50
      const rawVols  = volumes.map(v => v.value);
      const volMa50  = calcSMA(rawVols, 50);

      // Map EMAs to time series format
      const toTimeSeries = (timeArr, vals) =>
        timeArr.map((t, i) => ({ time: t, value: vals[i] })).filter(p => p.value !== null);

      const times = candles.map(c => c.time);

      // Set data
      candleSeries.setData(candles);
      ema10Series.setData(toTimeSeries(times, emaValues10));
      ema20Series.setData(toTimeSeries(times, emaValues20));
      ema50Series.setData(toTimeSeries(times, emaValues50));
      ema200Series.setData(toTimeSeries(times, emaValues200));

      volSeries.setData(volumes);
      volMaSeries.setData(
        times.map((t, i) => ({ time: t, value: volMa50[i] })).filter(p => p.value !== null)
      );

      // Fit content
      priceChart.timeScale().fitContent();
      volumeChart.timeScale().fitContent();

      // Update toolbar
      const last   = candles[candles.length - 1];
      const prev   = candles[candles.length - 2];
      const change = prev ? ((last.close - prev.close) / prev.close * 100) : 0;
      const sign   = change >= 0 ? '+' : '';

      chartSymName.textContent = symbol;
      chartPrice.textContent   = `₹${last.close.toLocaleString('en-IN', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
      chartChange.textContent  = `${sign}${change.toFixed(2)}%`;
      chartChange.className    = change >= 0 ? 'up' : 'down';

      // Document title
      document.title = `${symbol} ₹${last.close.toFixed(2)} — NSE Dashboard`;

      chartLoading.classList.remove('visible');
    } catch (err) {
      chartLoading.classList.remove('visible');
      showToast('❌', `Chart error: ${err.message}`, 'error');
      console.error('Chart render error:', err);
    }
  }

  /* ═══════════════════════════════════════════════════════════════════
     TIMEFRAME BUTTONS
  ═══════════════════════════════════════════════════════════════════ */
  document.querySelectorAll('.tf-btn').forEach(btn => {
    btn.addEventListener('click', () => {
      document.querySelectorAll('.tf-btn').forEach(b => b.classList.remove('active'));
      btn.classList.add('active');
      currentRange    = btn.dataset.range;
      currentInterval = btn.dataset.interval;
      if (activeSymbol) loadChart(activeSymbol, currentRange, currentInterval);
    });
  });

  /* ═══════════════════════════════════════════════════════════════════
     FILTER CHANGE
  ═══════════════════════════════════════════════════════════════════ */
  filterSelect.addEventListener('change', applyFilter);

  /* ═══════════════════════════════════════════════════════════════════
     REFRESH BUTTON & MODAL
  ═══════════════════════════════════════════════════════════════════ */
  refreshBtn.addEventListener('click', triggerRefresh);
  refreshModalClose.addEventListener('click', closeRefreshModal);
  refreshModal.addEventListener('click', e => {
    if (e.target === refreshModal) closeRefreshModal();
  });

  function triggerRefresh() {
    refreshBtn.disabled = true;
    refreshBtn.classList.add('spinning');
    openRefreshModal();

    fetch('/api/refresh', { method: 'POST' })
      .then(r => r.json())
      .then(data => {
        if (data.status === 'already_running') {
          appendLog('[WARN] A refresh is already running…', 'warn');
        } else {
          appendLog('[INFO] Refresh process started…');
          startRefreshPolling();
        }
      })
      .catch(err => {
        appendLog(`[ERROR] Could not start refresh: ${err.message}`, 'err');
        finishRefresh(false);
      });
  }

  function startRefreshPolling() {
    let lastLogCount = 0;
    refreshPollTimer = setInterval(() => {
      fetch('/api/refresh/status')
        .then(r => r.json())
        .then(data => {
          // Append new log lines
          const newLines = (data.log || []).slice(lastLogCount);
          newLines.forEach(line => {
            const cls = line.includes('[ERROR]') ? 'err' : line.includes('[WARN]') ? 'warn' : '';
            appendLog(line, cls);
          });
          lastLogCount = (data.log || []).length;

          if (!data.running) {
            clearInterval(refreshPollTimer);
            refreshPollTimer = null;

            if (data.ok) {
              appendLog('✅ Refresh complete! Reloading data…');
              refreshModalMsg.textContent = '✅ Refresh complete!';
              finishRefresh(true);
              setTimeout(() => {
                loadStocks();
                closeRefreshModal();
              }, 1500);
            } else {
              appendLog('❌ Refresh failed. Check the log above.', 'err');
              refreshModalMsg.textContent = '❌ Refresh failed';
              finishRefresh(false);
            }
          }
        })
        .catch(() => {});
    }, 1000);
  }

  function finishRefresh(success) {
    refreshBtn.disabled = false;
    refreshBtn.classList.remove('spinning');
    showToast(success ? '✅' : '❌', success ? 'Data refreshed!' : 'Refresh failed', success ? 'success' : 'error');
  }

  function openRefreshModal() {
    refreshLog.innerHTML = '';
    refreshModalMsg.textContent = 'Connecting to fetch_data.py…';
    refreshModal.classList.add('visible');
    document.body.style.overflow = 'hidden';
  }

  function closeRefreshModal() {
    if (refreshPollTimer) {
      clearInterval(refreshPollTimer);
      refreshPollTimer = null;
    }
    refreshModal.classList.remove('visible');
    document.body.style.overflow = '';
    finishRefresh(false);
  }

  function appendLog(line, cls) {
    const span = document.createElement('span');
    span.className = `log-line${cls ? ' log-' + cls : ''}`;
    span.textContent = line;
    refreshLog.appendChild(span);
    refreshLog.appendChild(document.createTextNode('\n'));
    refreshLog.scrollTop = refreshLog.scrollHeight;
    refreshModalMsg.textContent = line.slice(0, 60) + (line.length > 60 ? '…' : '');
  }

  /* ═══════════════════════════════════════════════════════════════════
     TOAST
  ═══════════════════════════════════════════════════════════════════ */
  let toastTimer = null;
  function showToast(icon, msg, type) {
    toastIcon.textContent = icon;
    toastMsg.textContent  = msg;
    toast.style.borderColor =
      type === 'success' ? 'rgba(16,185,129,0.4)' :
      type === 'error'   ? 'rgba(239,68,68,0.4)'  :
      'rgba(255,255,255,0.13)';
    toast.classList.add('show');
    if (toastTimer) clearTimeout(toastTimer);
    toastTimer = setTimeout(() => toast.classList.remove('show'), 3500);
  }

  /* ═══════════════════════════════════════════════════════════════════
     UTILS
  ═══════════════════════════════════════════════════════════════════ */
  function escHtml(s) {
    return String(s)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;');
  }

  /* ═══════════════════════════════════════════════════════════════════
     KEYBOARD SHORTCUT  (R = refresh, Escape = close modal)
  ═══════════════════════════════════════════════════════════════════ */
  document.addEventListener('keydown', e => {
    const noInput = document.activeElement.tagName !== 'INPUT'
                 && document.activeElement.tagName !== 'SELECT';
    if (e.key === 'Escape') {
      if (refreshModal.classList.contains('visible')) { closeRefreshModal(); return; }
      if (pendingPoint) { pendingPoint = null; if (previewPrim) previewPrim.clear();
        $('chart-body')?.classList.remove('pending-point'); return; }
      if (activeTool !== 'pointer') { setActiveTool('pointer'); return; }
    }
    if (e.key === 'z' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); undoLastDrawing(); }
    if (e.key === 'r' && noInput && !e.ctrlKey && !e.metaKey) { triggerRefresh(); }
  });

  /* ═══════════════════════════════════════════════════════════════════
     STARTUP
  ═══════════════════════════════════════════════════════════════════ */
  function init() {
    initCharts();
    loadStocks();
    initDrawingToolbar();
  }

  init();
})();

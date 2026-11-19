/* Progressive enhancement: preserve the server-rendered chart values. */
(() => {
  const plot = document.getElementById('overview-trend');
  if (!plot) return;
  const fallback = plot.nextElementSibling;
  const columns = [...fallback.querySelectorAll('.sales-bar-column')];
  const values = columns.map(column => Number(column.dataset.value));
  if (!values.length || values.some(value => !Number.isFinite(value))) return;
  const ns = 'http://www.w3.org/2000/svg';
  function draw() {
    const width = plot.clientWidth;
    if (width < 180) return;
    const height = 265, left = width < 450 ? 62 : 85, right = 22, top = 20, bottom = 43;
    const low = Math.min(0, ...values), high = Math.max(0, ...values), span = high - low || 1;
    const min = low < 0 ? low - span * .08 : 0;
    const max = high > 0 ? high + span * .15 : low < 0 ? 0 : 1;
    const y = value => top + (max - value) / (max - min) * (height - top - bottom);
    const x = index => left + index / Math.max(1, values.length - 1) * (width - left - right);
    const svg = document.createElementNS(ns, 'svg');
    svg.setAttribute('viewBox', `0 0 ${width} ${height}`);
    svg.setAttribute('width', width); svg.setAttribute('height', height);
    const add = (tag, attrs, text) => {
      const element = document.createElementNS(ns, tag);
      Object.entries(attrs).forEach(([name, value]) => element.setAttribute(name, value));
      if (text !== undefined) element.textContent = text;
      svg.appendChild(element); return element;
    };
    const format = value => new Intl.NumberFormat(undefined, {maximumFractionDigits:0, notation:width < 450 ? 'compact' : 'standard'}).format(value);
    add('text', {x:left, y:12, class:'trend-label'}, plot.dataset.currency);
    for (let tick = 0; tick <= 4; tick++) {
      const value = min + (max - min) * tick / 4;
      add('line', {x1:left, x2:width-right, y1:y(value), y2:y(value), class:'trend-grid'});
      add('text', {x:left-10, y:y(value)+4, 'text-anchor':'end', class:'trend-label'}, format(value));
    }
    const points = values.map((value, index) => `${x(index)},${y(value)}`).join(' ');
    add('polygon', {points:`${x(0)},${y(0)} ${points} ${x(values.length-1)},${y(0)}`, class:'trend-area'});
    add('polyline', {points, fill:'none', class:'trend-line'});
    columns.forEach((column, index) => {
      const dot = add('circle', {cx:x(index), cy:y(values[index]), r:4, class:'trend-dot'});
      const title = document.createElementNS(ns, 'title');
      title.textContent = column.title; dot.appendChild(title);
      add('text', {x:x(index), y:height-21, 'text-anchor':'middle', class:'trend-label'}, column.querySelector('.sales-bar-day').textContent);
      if (width > 420) add('text', {x:x(index), y:height-5, 'text-anchor':'middle', class:'trend-date'}, column.querySelector('.sales-bar-date').textContent);
    });
    plot.replaceChildren(svg);
    fallback.classList.add('trend-data');
  }
  draw();
  if (globalThis.ResizeObserver) new ResizeObserver(draw).observe(plot);
  else window.addEventListener('resize', draw);
})();

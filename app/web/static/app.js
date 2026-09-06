(() => {
  document.documentElement.classList.add('motion-ready');
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  const format = new Intl.NumberFormat('en-US');

  function animateNumber(element) {
    const finalValue = Number(element.dataset.count || 0);
    if (!Number.isFinite(finalValue) || reducedMotion) {
      element.textContent = format.format(finalValue);
      return;
    }

    const duration = 850;
    const start = performance.now();
    const easeOut = (value) => 1 - Math.pow(1 - value, 4);
    element.textContent = '0';

    function frame(now) {
      const progress = Math.min((now - start) / duration, 1);
      element.textContent = format.format(Math.round(finalValue * easeOut(progress)));
      if (progress < 1) requestAnimationFrame(frame);
    }

    requestAnimationFrame(frame);
  }

  document.querySelectorAll('[data-count]').forEach(animateNumber);

  const revealItems = document.querySelectorAll('[data-reveal]');
  if (reducedMotion || !('IntersectionObserver' in window)) {
    revealItems.forEach((item) => item.classList.add('is-visible'));
  } else {
    const observer = new IntersectionObserver((entries) => {
      entries.forEach((entry) => {
        if (!entry.isIntersecting) return;
        entry.target.classList.add('is-visible');
        observer.unobserve(entry.target);
      });
    }, { threshold: 0.08 });
    revealItems.forEach((item) => observer.observe(item));
  }

  document.querySelectorAll('[data-auto-submit] select').forEach((select) => {
    select.addEventListener('change', () => {
      document.body.classList.add('is-navigating');
      select.form.requestSubmit();
    });
  });

  document.querySelectorAll('.refresh-form').forEach((form) => {
    form.addEventListener('submit', () => {
      const button = form.querySelector('button');
      button?.classList.add('is-busy');
      if (button) button.disabled = true;
    });
  });

  // Charts — read data from data-chart attributes to keep CSP script-src 'self'.
  function parseChartData(element) {
    const raw = element.getAttribute('data-chart') || element.dataset.chart;
    if (!raw) return null;
    try {
      return JSON.parse(raw);
    } catch {
      return null;
    }
  }

  const trendCanvas = document.getElementById('trendChart');
  if (trendCanvas && typeof window.Chart !== 'undefined') {
    const data = parseChartData(trendCanvas);
    if (data && Array.isArray(data.labels)) {
      const motionOK = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      const styles = getComputedStyle(document.documentElement);
      const chartColors = {
        reach: styles.getPropertyValue('--cyan').trim(),
        reactions: styles.getPropertyValue('--coral').trim(),
        comments: styles.getPropertyValue('--teal').trim(),
        shares: styles.getPropertyValue('--blue').trim(),
        muted: styles.getPropertyValue('--muted').trim(),
        grid: styles.getPropertyValue('--chart-grid').trim(),
      };
      const trendChart = new window.Chart(trendCanvas, {
        type: 'line',
        data: {
          labels: data.labels,
          datasets: [
            { label: 'Reach / Views', data: data.views, borderColor: chartColors.reach, backgroundColor: 'rgba(26, 211, 238, .10)', fill: true, tension: 0.36, borderWidth: 2.5, pointRadius: 2, pointHoverRadius: 6 },
            { label: 'Reactions', data: data.reactions, borderColor: chartColors.reactions, tension: 0.36, borderWidth: 2, pointRadius: 1.5, pointHoverRadius: 5 },
            { label: 'Comments', data: data.comments, borderColor: chartColors.comments, tension: 0.36, borderWidth: 2, pointRadius: 1.5, pointHoverRadius: 5 },
            { label: 'Shares', data: data.shares, borderColor: chartColors.shares, tension: 0.36, borderWidth: 2, pointRadius: 1.5, pointHoverRadius: 5 },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          animation: motionOK ? { duration: 1400, easing: 'easeOutQuart' } : false,
          plugins: {
            legend: { position: 'top', align: 'start', labels: { color: chartColors.muted, boxWidth: 24, boxHeight: 2, padding: 24, usePointStyle: false, font: { family: 'Manrope Variable', size: 12, weight: 550 } } },
            tooltip: { backgroundColor: '#0d1a27', borderColor: 'rgba(119, 151, 177, .3)', borderWidth: 1, titleColor: '#f4f8fb', bodyColor: '#a6b5c3', padding: 12, displayColors: true },
          },
          scales: {
            x: { ticks: { color: chartColors.muted, maxTicksLimit: 12, font: { family: 'Manrope Variable', size: 11 } }, grid: { display: false }, border: { display: false } },
            y: { beginAtZero: true, ticks: { color: chartColors.muted, padding: 12, font: { family: 'Manrope Variable', size: 11 } }, grid: { color: chartColors.grid, borderDash: [4, 5] }, border: { display: false } },
          },
        },
      });

      const rawSeries = {
        labels: [...data.labels],
        values: [data.views, data.reactions, data.comments, data.shares].map((s) => [...s]),
      };
      function groupedIndexes(period) {
        if (period === 'day') return rawSeries.labels.map((_, i) => i);
        const lastByPeriod = new Map();
        rawSeries.labels.forEach((label, index) => {
          const date = new Date(`${label}T00:00:00Z`);
          let key;
          if (period === 'month') {
            key = label.slice(0, 7);
          } else {
            const mondayOffset = (date.getUTCDay() + 6) % 7;
            date.setUTCDate(date.getUTCDate() - mondayOffset);
            key = date.toISOString().slice(0, 10);
          }
          lastByPeriod.set(key, index);
        });
        return [...lastByPeriod.values()];
      }
      document.querySelectorAll('[data-chart-period]').forEach((button) => {
        button.addEventListener('click', () => {
          const indexes = groupedIndexes(button.dataset.chartPeriod);
          trendChart.data.labels = indexes.map((i) => rawSeries.labels[i]);
          trendChart.data.datasets.forEach((dataset, si) => {
            dataset.data = indexes.map((i) => rawSeries.values[si][i]);
          });
          document.querySelectorAll('[data-chart-period]').forEach((item) => {
            const selected = item === button;
            item.classList.toggle('active', selected);
            item.setAttribute('aria-pressed', String(selected));
          });
          trendChart.update(motionOK ? undefined : 'none');
        });
      });
    }
  }

  const postCanvas = document.getElementById('postChart');
  if (postCanvas && typeof window.Chart !== 'undefined') {
    const data = parseChartData(postCanvas);
    if (data && Array.isArray(data.labels)) {
      const motionOK = !window.matchMedia('(prefers-reduced-motion: reduce)').matches;
      const styles = getComputedStyle(document.documentElement);
      const colors = {
        reach: styles.getPropertyValue('--cyan').trim(),
        reactions: styles.getPropertyValue('--coral').trim(),
        comments: styles.getPropertyValue('--teal').trim(),
        shares: styles.getPropertyValue('--blue').trim(),
        muted: styles.getPropertyValue('--muted').trim(),
        grid: styles.getPropertyValue('--chart-grid').trim(),
      };
      new window.Chart(postCanvas, {
        type: 'line',
        data: {
          labels: data.labels,
          datasets: [
            { label: 'Reach / Views', data: data.views, borderColor: colors.reach, backgroundColor: 'rgba(25, 210, 236, .10)', fill: true, tension: 0.36, borderWidth: 2.5, pointRadius: 2, pointHoverRadius: 6 },
            { label: 'Reactions', data: data.reactions, borderColor: colors.reactions, tension: 0.36, borderWidth: 2, pointRadius: 1.5, pointHoverRadius: 5 },
            { label: 'Comments', data: data.comments, borderColor: colors.comments, tension: 0.36, borderWidth: 2, pointRadius: 1.5, pointHoverRadius: 5 },
            { label: 'Shares', data: data.shares, borderColor: colors.shares, tension: 0.36, borderWidth: 2, pointRadius: 1.5, pointHoverRadius: 5 },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          interaction: { mode: 'index', intersect: false },
          animation: motionOK ? { duration: 1300, easing: 'easeOutQuart' } : false,
          plugins: {
            legend: { position: 'top', align: 'start', labels: { color: colors.muted, boxWidth: 24, boxHeight: 2, padding: 24, font: { family: 'Manrope Variable', size: 12, weight: 550 } } },
            tooltip: { backgroundColor: '#0d1a27', borderColor: 'rgba(119, 151, 177, .3)', borderWidth: 1, titleColor: '#f4f8fb', bodyColor: '#a6b5c3', padding: 12 },
          },
          scales: {
            x: { ticks: { color: colors.muted, maxTicksLimit: 10, font: { family: 'Manrope Variable', size: 11 } }, grid: { display: false }, border: { display: false } },
            y: { beginAtZero: true, ticks: { color: colors.muted, padding: 12, font: { family: 'Manrope Variable', size: 11 } }, grid: { color: colors.grid, borderDash: [4, 5] }, border: { display: false } },
          },
        },
      });
    }
  }
})();

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

  function activateSaveConfirmation(saveConfirmation) {
    if (!saveConfirmation) return;
    const section = saveConfirmation.dataset.section;
    const target = section ? document.getElementById(section) : null;
    const savedButton = target?.querySelector('.form-actions button[type="submit"]');
    const originalButtonText = savedButton?.textContent;
    let dismissed = false;

    const dismiss = () => {
      if (dismissed) return;
      dismissed = true;
      saveConfirmation.classList.add('is-leaving');
      window.setTimeout(() => saveConfirmation.remove(), reducedMotion ? 0 : 360);
    };

    requestAnimationFrame(() => {
      saveConfirmation.classList.add('is-visible');
      if (savedButton) {
        savedButton.textContent = 'Saved';
        savedButton.classList.add('is-saved');
      }
    });

    window.setTimeout(() => {
      if (savedButton && originalButtonText) {
        savedButton.textContent = originalButtonText;
        savedButton.classList.remove('is-saved');
      }
    }, reducedMotion ? 0 : 1900);
    window.setTimeout(dismiss, reducedMotion ? 4000 : 4200);
    saveConfirmation.querySelector('.save-confirmation-close')?.addEventListener('click', dismiss);
  }

  activateSaveConfirmation(document.querySelector('[data-save-confirmation]'));

  const settingsStack = document.querySelector('.settings-stack');
  if (settingsStack) {
    settingsStack.addEventListener('submit', async (event) => {
      const form = event.target;
      if (!(form instanceof HTMLFormElement) || form.method.toLowerCase() !== 'post') return;

      const action = new URL(form.action, window.location.href);
      if (action.origin !== window.location.origin || !action.pathname.startsWith('/settings/')) return;

      event.preventDefault();
      const panel = form.closest('.panel');
      if (!panel?.id || form.dataset.submitting === 'true') return;

      form.dataset.submitting = 'true';
      const submitter = event.submitter instanceof HTMLButtonElement ? event.submitter : null;
      if (submitter) {
        submitter.disabled = true;
        submitter.classList.add('is-busy');
      }

      try {
        const response = await fetch(action, {
          method: 'POST',
          body: new FormData(form),
          credentials: 'same-origin',
          headers: { Accept: 'text/html' },
        });
        const responseURL = new URL(response.url, window.location.href);
        if (response.redirected && (responseURL.origin !== window.location.origin || responseURL.pathname !== '/settings')) {
          window.location.assign(response.url);
          return;
        }

        if (!response.headers.get('content-type')?.includes('text/html')) {
          throw new Error('Settings response is not HTML.');
        }
        const html = await response.text();
        const nextDocument = new DOMParser().parseFromString(html, 'text/html');
        const nextPanel = nextDocument.getElementById(panel.id);
        if (!nextPanel) throw new Error('Updated settings section is missing from the response.');

        const updatedContents = [...nextPanel.childNodes].map((node) => document.importNode(node, true));
        panel.replaceChildren(...updatedContents);

        const currentConfirmation = document.querySelector('[data-save-confirmation]');
        currentConfirmation?.remove();
        const nextConfirmation = nextDocument.querySelector('[data-save-confirmation]');
        if (nextConfirmation) {
          const confirmation = document.importNode(nextConfirmation, true);
          document.getElementById('main-content')?.prepend(confirmation);
          activateSaveConfirmation(confirmation);
        }

        panel.querySelector('[aria-invalid="true"]')?.focus({ preventScroll: true });
      } catch {
        form.dataset.submitting = 'false';
        if (submitter) {
          submitter.disabled = false;
          submitter.classList.remove('is-busy');
        }
        panel.querySelector('[data-settings-save-error]')?.remove();
        const message = document.createElement('div');
        message.className = 'banner warn';
        message.dataset.settingsSaveError = '';
        message.setAttribute('role', 'alert');
        message.textContent = 'Could not save these settings. Check the connection and try again.';
        panel.prepend(message);
      }
    });
  }

  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-delete-dialog-open]');
    if (button instanceof HTMLButtonElement) {
      const dialog = document.getElementById(button.dataset.deleteDialogOpen || '');
      if (!(dialog instanceof HTMLDialogElement)) return;
      dialog.showModal();
      dialog.querySelector('input[name="confirmation"]')?.focus();
      return;
    }

    const closeButton = event.target.closest('[data-delete-dialog-close]');
    if (closeButton instanceof HTMLButtonElement) {
      closeButton.closest('dialog')?.close();
      return;
    }

    if (event.target instanceof HTMLDialogElement && event.target.matches('.delete-channel-dialog')) {
      event.target.close();
    }
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

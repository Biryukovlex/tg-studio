# License and notices

TG Studio is licensed under the Apache License, Version 2.0. The complete
terms are in the root [`LICENSE`](../LICENSE) file and attribution notices are
in [`NOTICE`](../NOTICE). Contributions intentionally submitted to the project
are accepted under the same license unless explicitly stated otherwise.

Runtime and frontend dependencies are installed from the lock/range manifests.
Run the release checks before publishing an image:

```bash
.venv/bin/python scripts/check_dependency_licenses.py
.venv/bin/python -m pip_audit -r requirements.txt
npm --prefix studio-frontend audit --omit=dev --audit-level=high
```

The dashboard ships the Manrope and MingCute font assets under
`app/web/static/vendor/`; their license notices remain beside the vendored
files and are summarized in the root `NOTICE`. Chart.js 4.4.1 (`app/web/static/vendor/chartjs/chart.umd.min.js`)
is vendored under the MIT License — Copyright (c) 2023 Chart.js Contributors,
https://www.chartjs.org, `https://github.com/chartjs/Chart.js/blob/master/LICENSE.md`.
Assistant UI, AG-UI, React, and Vite notices are supplied by their npm packages and must be retained in any
redistributed frontend bundle.

SearXNG is not linked into the Python image. It is an optional Compose service
under the GNU Affero General Public License (AGPL). Operators who run or
modify it must review the selected image version, corresponding source-offer
requirements, upstream engine terms, and their own distribution obligations.
Pin and document the image tag or digest for a hosted release.

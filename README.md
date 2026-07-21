# VoltBridge — 800VDC HIL Bench

<!-- Replace jitharav/voltbridge with your GitHub username and repository name -->
[![CI](https://github.com/jitharav/voltbridge/actions/workflows/ci.yml/badge.svg)](https://github.com/jitharav/voltbridge/actions/workflows/ci.yml)
[![Deploy](https://github.com/jitharav/voltbridge/actions/workflows/deploy.yml/badge.svg)](https://github.com/jitharav/voltbridge/actions/workflows/deploy.yml)

**Live dashboard:** https://jitharav.github.io/voltbridge/  (standalone simulation)

One simulated 800V DC power stage, two domains: **EV fast-charging** and **AI data-center racks**.
Live physics + an IS 17017 / ACAN charging state machine + reactive protection trips.

## Run locally

Requires Node.js 18+.

```bash
npm install
npm run dev
```

Then open the URL Vite prints (default http://localhost:5173).

Build a static bundle for deploying/presenting offline:

```bash
npm run build
npm run preview
```

## Demo flow

1. Press **START** — the sequence runs handshake → insulation → precharge → energy transfer.
2. Mid–energy-transfer, click **Insulation** under *Inject fault* — the contactor opens, current
   collapses, and the emergency-stop frame posts on the ACAN log.
3. Switch the toggle to **AI Data Center** (from Standby) to run the same core as a 1 MW rack.

## Project layout

```
voltbridge/
  index.html
  package.json
  vite.config.js
  src/
    main.jsx          # React entry
    VoltBridge.jsx    # the whole simulator (single component)
```

## Notes

- CAN IDs and payloads in `VoltBridge.jsx` are representative — swap in your real DBC / IS 17017-24
  message set in the `CAN_NAMES` map.
- Protection thresholds live in the `LIMIT` object; physics constants are in the `step()` function.

import React, { useEffect, useRef, useState, useCallback } from "react";
import {
  LineChart,
  Line,
  XAxis,
  YAxis,
  ResponsiveContainer,
  ReferenceLine,
} from "recharts";

/* ------------------------------------------------------------------ *
 * VoltBridge — 800VDC Hardware-in-the-Loop Bench
 * One simulated 800V DC power stage, two domains: EV fast-charge and
 * AI-factory rack. Live physics + IS 17017 / ACAN state machine +
 * protection trips. Self-contained; no backend.
 * ------------------------------------------------------------------ */

const C = {
  bg: "#05070B", // near-black so panels pop
  panel: "#18232F", // clearly lighter than bg
  panel2: "#1E2B39",
  line: "#38506690", // brighter borders for separation
  lineSoft: "#2A3B4D",
  text: "#FFFFFF", // pure white primary
  muted: "#C2D0DC", // bright secondary (was dim)
  faint: "#A3B4C2", // readable tertiary (brightened for legibility)
  volt: "#FFC13A", // bus voltage — bright gold
  amp: "#3ADCF2", // current — bright cyan
  power: "#BCA4FF", // power — bright violet
  ok: "#42E27E", // nominal — bright green
  warn: "#FFA23D", // warning — bright orange
  fault: "#FF5D5D", // trip — bright red
  evAccent: "#2FEDB6", // EV mode accent
  dcAccent: "#FFC13A", // data-center mode accent
};

// ---- Phases ----
const PHASE = {
  IDLE: "IDLE",
  HANDSHAKE: "HANDSHAKE",
  INSULATION: "INSULATION",
  PRECHARGE: "PRECHARGE",
  TRANSFER: "TRANSFER",
  FAULT: "FAULT",
  SHUTDOWN: "SHUTDOWN",
  COMPLETE: "COMPLETE",
};

const SEQUENCE = [
  PHASE.HANDSHAKE,
  PHASE.INSULATION,
  PHASE.PRECHARGE,
  PHASE.TRANSFER,
];

const PHASE_LABEL = {
  IDLE: "Standby",
  HANDSHAKE: "Handshake",
  INSULATION: "Insulation test",
  PRECHARGE: "Precharge",
  TRANSFER: "Energy transfer",
  FAULT: "Protection trip",
  SHUTDOWN: "Safe shutdown",
  COMPLETE: "Charge complete",
};

const PHASE_DUR = {
  HANDSHAKE: 2.0,
  INSULATION: 2.6,
  PRECHARGE: 1.6,
  SHUTDOWN: 2.0,
};

// ---- Protection limits ----
const LIMIT = {
  vBusMax: 900, // V
  isoMinMohm: 0.1, // 100 kOhm
  tempMaxC: 85, // C
  commsTimeoutS: 1.2,
};

const DT = 0.1; // 100 ms tick

function fresh(mode) {
  return {
    t: 0,
    phase: PHASE.IDLE,
    phaseT: 0,
    mode, // "ev" | "dc"
    vBus: 0,
    iBus: 0,
    temp: 25,
    eff: 0,
    iso: 999, // MOhm
    soc: 22, // EV %
    load: 0, // DC %
    contactor: false, // closed?
    faultCode: null,
    faultName: null,
    injected: null, // pending fault key
    lastRxT: 0,
    canId: 0,
    can: [], // message log (newest first)
    hist: [], // {t, v, i}
  };
}

const CAN_NAMES = {
  ev: {
    hsA: ["0x100", "EVSE_Handshake"],
    hsB: ["0x101", "EV_Handshake"],
    isoCmd: ["0x200", "Insulation_Test_Cmd"],
    isoSt: ["0x201", "Insulation_Status"],
    pre: ["0x300", "Precharge_Cmd"],
    param: ["0x301", "Charge_Parameters"],
    req: ["0x302", "Charge_Request"],
    stat: ["0x303", "Charge_Status"],
    stop: ["0x400", "Stop_Charge"],
    estop: ["0x7FF", "Emergency_Stop"],
  },
  dc: {
    hsA: ["0x100", "PSU_Handshake"],
    hsB: ["0x101", "Rack_Handshake"],
    isoCmd: ["0x200", "Insulation_Test_Cmd"],
    isoSt: ["0x201", "Insulation_Status"],
    pre: ["0x300", "Bus_Precharge_Cmd"],
    param: ["0x301", "Power_Envelope"],
    req: ["0x302", "Power_Request"],
    stat: ["0x303", "Rail_Status"],
    stop: ["0x400", "Power_Down"],
    estop: ["0x7FF", "Emergency_Stop"],
  },
};

const hx = (n) =>
  Math.max(0, Math.min(255, Math.round(n)))
    .toString(16)
    .toUpperCase()
    .padStart(2, "0");

// PMBus LINEAR11 encode -> "hi lo" hex (matches the Python bench)
function lin11(value) {
  let best = null;
  for (let nn = -16; nn < 16; nn++) {
    let y = Math.round(value / Math.pow(2, nn));
    if (y >= -1024 && y <= 1023) {
      const err = Math.abs(y * Math.pow(2, nn) - value);
      if (best === null || err < best.err) best = { err, y, n: nn };
    }
  }
  const raw = (((best.n & 0x1f) << 11) | (best.y & 0x7ff)) & 0xffff;
  return hx((raw >> 8) & 0xff) + " " + hx(raw & 0xff);
}

// CRC-16/Modbus over a byte array -> "lo hi" hex (real Modbus byte order)
function modbusCrc(bytes) {
  let crc = 0xffff;
  for (const b of bytes) {
    crc ^= b;
    for (let i = 0; i < 8; i++) crc = crc & 1 ? (crc >> 1) ^ 0xa001 : crc >> 1;
  }
  return hx(crc & 0xff) + " " + hx((crc >> 8) & 0xff);
}

// Real-world EV models (approximate published specs). Each drives the charge:
// pack voltage (packV) -> bus target, peak current (iMax) -> gauge/trace,
// pack size (kwh) -> demo time-to-full (fullSec). 400V cars pull ~2x the current
// of 800V cars for similar power - the core reason for the 800V transition.
const EV_MODELS = [
  // 800V architecture
  { id: "taycan",  name: "Porsche Taycan Turbo S", region: "German · 800V",  kwh: 93,  packV: 800, iMax: 338, fullSec: 33, color: "#38E1C6", body: "coupe" },
  { id: "etrongt", name: "Audi e-tron GT",          region: "German · 800V",  kwh: 93,  packV: 800, iMax: 338, fullSec: 33, color: "#E8543B", body: "coupe" },
  { id: "folgore", name: "Maserati GranTurismo Folgore", region: "Italian · 800V", kwh: 83, packV: 800, iMax: 338, fullSec: 30, color: "#3B6BE8", body: "coupe" },
  { id: "battista",name: "Pininfarina Battista",    region: "Italian · 800V", kwh: 120, packV: 800, iMax: 338, fullSec: 42, color: "#E8C13B", body: "coupe" },
  { id: "ferrari", name: "Ferrari Elettrica (est.)",region: "Italian · 800V", kwh: 110, packV: 800, iMax: 437, fullSec: 36, color: "#E5342B", body: "coupe" },
  { id: "ioniq5",  name: "Hyundai Ioniq 5",         region: "Korean · 800V",  kwh: 77,  packV: 800, iMax: 294, fullSec: 24, color: "#7FC9E8", body: "suv" },
  // 400V architecture
  { id: "models",  name: "Tesla Model S Plaid",     region: "USA · 400V",     kwh: 100, packV: 400, iMax: 625, fullSec: 35, color: "#C9CED6", body: "sedan" },
  { id: "model3",  name: "Tesla Model 3 Long Range",region: "USA · 400V",     kwh: 75,  packV: 400, iMax: 625, fullSec: 27, color: "#B23A3A", body: "sedan" },
  { id: "i7",      name: "BMW i7 M70",              region: "German · 400V",  kwh: 105, packV: 400, iMax: 488, fullSec: 38, color: "#4C7EF0", body: "sedan" },
  { id: "eqs",     name: "Mercedes-AMG EQS",        region: "German · 400V",  kwh: 108, packV: 400, iMax: 500, fullSec: 38, color: "#9AA6B2", body: "sedan" },
  { id: "ariya",   name: "Nissan Ariya",            region: "Japanese · 400V",kwh: 87,  packV: 400, iMax: 325, fullSec: 31, color: "#F0902E", body: "suv" },
  { id: "bz4x",    name: "Toyota bZ4X",             region: "Japanese · 400V",kwh: 71,  packV: 400, iMax: 375, fullSec: 26, color: "#4CC46A", body: "suv" },
  { id: "lexusrz", name: "Lexus RZ 450e",           region: "Japanese · 400V",kwh: 71,  packV: 400, iMax: 375, fullSec: 26, color: "#A96BE8", body: "suv" },
];
const evKW = (m) => Math.round((m.packV * m.iMax) / 1000);

// AI accelerators. Power figures are approximate; (est.) marks unpublished/estimated.
// dense=true are the high-density racks driving the 800VDC transition; the others
// run on today's 48/54V busbars and are shown for contrast.
const AI_CHIPS = [
  // NVIDIA
  { id: "h100",   name: "NVIDIA H100 (Hopper)",        vendor: "NVIDIA", color: "#76B900", wGpu: 700,  gpus: 32,  rackKW: 45,  dense: false },
  { id: "h200",   name: "NVIDIA H200 (Hopper)",        vendor: "NVIDIA", color: "#76B900", wGpu: 700,  gpus: 32,  rackKW: 48,  dense: false },
  { id: "gb200",  name: "NVIDIA GB200 NVL72 (Blackwell)", vendor: "NVIDIA", color: "#76B900", wGpu: 1200, gpus: 72, rackKW: 120, dense: true },
  { id: "gb300",  name: "NVIDIA GB300 NVL72 (Blackwell Ultra)", vendor: "NVIDIA", color: "#76B900", wGpu: 1400, gpus: 72, rackKW: 140, dense: true },
  { id: "rubin",  name: "NVIDIA Vera Rubin NVL144 (est.)", vendor: "NVIDIA", color: "#76B900", wGpu: 1800, gpus: 144, rackKW: 600, dense: true, est: true },
  // AMD
  { id: "mi300x", name: "AMD Instinct MI300X",         vendor: "AMD", color: "#ED1C24", wGpu: 750,  gpus: 48, rackKW: 45,  dense: false },
  { id: "mi325x", name: "AMD Instinct MI325X",         vendor: "AMD", color: "#ED1C24", wGpu: 1000, gpus: 48, rackKW: 60,  dense: true },
  { id: "mi355x", name: "AMD Instinct MI355X (est.)",  vendor: "AMD", color: "#ED1C24", wGpu: 1400, gpus: 64, rackKW: 100, dense: true, est: true },
  // Google
  { id: "tpu5p",  name: "Google TPU v5p (est.)",       vendor: "Google", color: "#4285F4", wGpu: 450, gpus: 64, rackKW: 40, dense: false, est: true },
  { id: "tpu6",   name: "Google TPU v6 Trillium (est.)", vendor: "Google", color: "#4285F4", wGpu: 700, gpus: 64, rackKW: 55, dense: true, est: true },
  // Amazon
  { id: "trn2",   name: "AWS Trainium2 (est.)",        vendor: "Amazon", color: "#FF9900", wGpu: 550, gpus: 64, rackKW: 45, dense: false, est: true },
  // Huawei
  { id: "asc910", name: "Huawei Ascend 910C (est.)",   vendor: "Huawei", color: "#CF0A2C", wGpu: 800, gpus: 64, rackKW: 90, dense: true, est: true },
  { id: "cloudm", name: "Huawei CloudMatrix 384 (est.)", vendor: "Huawei", color: "#CF0A2C", wGpu: 800, gpus: 384, rackKW: 560, dense: true, est: true },
  // Heterogeneous power profiles (power-delivery view: the 800VDC stage is
  // silicon-agnostic; single training jobs still run on homogeneous silicon).
  { id: "mix_nv_amd", name: "NVIDIA GB200 + AMD MI355X", vendor: "Mixed", color: "#76B900", colorB: "#ED1C24", wGpu: 1300, gpus: 68, rackKW: 110, dense: true, mix: true },
  { id: "mix_nv_tpu", name: "NVIDIA H200 + Google TPU v6", vendor: "Mixed", color: "#76B900", colorB: "#4285F4", wGpu: 700, gpus: 64, rackKW: 52, dense: true, mix: true },
  { id: "mix_amd_hw", name: "AMD MI355X + Huawei Ascend", vendor: "Mixed", color: "#ED1C24", colorB: "#CF0A2C", wGpu: 1100, gpus: 64, rackKW: 95, dense: true, mix: true },
];

export default function VoltBridge() {
  const sim = useRef(fresh("ev"));
  const [, setTick] = useState(0);
  const flash = useRef(0); // fault flash timer
  const rmMotion = useRef(false);
  const liveRef = useRef(false); // true while connected to the Python bench
  const modelRef = useRef(EV_MODELS[0]); // selected EV model (default: Taycan, 800V)
  const [evModel, setEvModel] = useState(EV_MODELS[0]);
  const selectModel = (m) => { modelRef.current = m; setEvModel(m); };
  const chipRef = useRef(AI_CHIPS[2]); // selected AI chip (default: GB200 NVL72)
  const [aiChip, setAiChip] = useState(AI_CHIPS[2]);
  const selectChip = (c) => { chipRef.current = c; setAiChip(c); };

  useEffect(() => {
    rmMotion.current =
      typeof window !== "undefined" &&
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }, []);

  const pushRow = (s, row) => {
    s.can.unshift({ k: s.canId++, t: s.t, ...row });
    if (s.can.length > 60) s.can.length = 60;
  };

  const pushCan = (s, key, bytes, dir) => {
    if (liveRef.current) return; // connected: the bench streams the real frames
    if (s.mode === "dc") return pushDc(s, key, dir);
    const map = CAN_NAMES.ev[key];
    if (!map) return;
    pushRow(s, { proto: "CAN", id: map[0], name: map[1], data: bytes.map(hx).join(" "), dir });
  };

  // Translate lifecycle events into PMBus (power) + Modbus (battery) rows for DC mode
  const pushDc = (s, key, dir) => {
    const V = Math.round(s.vBus), I = Math.round(s.iBus), P = Math.round(s.vBus * s.iBus);
    const A = "@0x42"; // rectifier / rack power stage
    switch (key) {
      case "hsA":
        pushRow(s, { proto: "PMBus", id: A, name: "READ_VIN", data: lin11(0), dir: "tx" }); break;
      case "pre":
        pushRow(s, { proto: "PMBus", id: A, name: "OPERATION on", data: "80", dir: "tx" }); break;
      case "param":
        pushRow(s, { proto: "PMBus", id: A, name: "VOUT_COMMAND", data: lin11(800), dir: "tx" }); break;
      case "stop":
        pushRow(s, { proto: "PMBus", id: A, name: "OPERATION off", data: "00", dir: "tx" }); break;
      case "estop":
        pushRow(s, { proto: "PMBus", id: A, name: "STATUS_WORD", data: "08 00", dir: "tx" });
        pushRow(s, { proto: "MODBUS", id: "0x01", name: "Alarm_Flags set", data: "--", dir: "tx" });
        break;
      case "stat": {
        // full telemetry burst — PMBus reads on power, Modbus read on battery
        pushRow(s, { proto: "PMBus", id: A, name: "READ_VOUT", data: lin11(V), dir: "tx" });
        pushRow(s, { proto: "PMBus", id: A, name: "READ_IOUT", data: lin11(I), dir: "tx" });
        pushRow(s, { proto: "PMBus", id: A, name: "READ_POUT", data: lin11(P), dir: "tx" });
        pushRow(s, { proto: "PMBus", id: A, name: "READ_TEMP_1", data: lin11(Math.round(s.temp)), dir: "tx" });
        const soc = Math.round(s.load ? s.load : 80);
        const frame = [0x01, 0x04, 0x0c, (soc >> 8) & 0xff, soc & 0xff];
        pushRow(s, { proto: "MODBUS", id: "0x01", name: "FC04 30001+6", data: "CRC " + modbusCrc(frame), dir: "tx" });
        break;
      }
      default: break; // hsB, isoCmd, isoSt, req -> no DC equivalent
    }
  };

  const trip = (s, code, name) => {
    if (s.phase === PHASE.FAULT) return;
    s.phase = PHASE.FAULT;
    s.phaseT = 0;
    s.faultCode = code;
    s.faultName = name;
    s.contactor = false;
    pushCan(s, "estop", [0x01, 0xff, 0x00, 0x00], "tx");
    flash.current = 1.4;
  };

  const step = useCallback(() => {
    const s = sim.current;
    s.t += DT;
    s.phaseT += DT;
    if (flash.current > 0) flash.current = Math.max(0, flash.current - DT);

    const isDC = s.mode === "dc";
    const maxP = isDC ? chipRef.current.rackKW * 1000 : 240_000; // W
    const effBase = isDC ? 98.6 : 97.4;

    // ----- phase transitions -----
    if (s.phase === PHASE.HANDSHAKE && s.phaseT >= PHASE_DUR.HANDSHAKE) {
      s.phase = PHASE.INSULATION;
      s.phaseT = 0;
      pushCan(s, "isoCmd", [0x01, 0x00], "tx");
    } else if (s.phase === PHASE.INSULATION && s.phaseT >= PHASE_DUR.INSULATION) {
      s.phase = PHASE.PRECHARGE;
      s.phaseT = 0;
      s.contactor = true;
      pushCan(s, "pre", [0x01], "tx");
      pushCan(
        s,
        "param",
        isDC
          ? [0x03, 0x20, 0x04, 0xe2]
          : [(modelRef.current.packV >> 8) & 0xff, modelRef.current.packV & 0xff,
             (modelRef.current.iMax >> 8) & 0xff, modelRef.current.iMax & 0xff],
        "tx"
      );
    } else if (s.phase === PHASE.PRECHARGE && s.phaseT >= PHASE_DUR.PRECHARGE) {
      s.phase = PHASE.TRANSFER;
      s.phaseT = 0;
    } else if (s.phase === PHASE.SHUTDOWN && s.phaseT >= PHASE_DUR.SHUTDOWN) {
      const keepMode = s.mode;
      sim.current = fresh(keepMode);
      return;
    }

    // ----- comms watchdog (ACAN timeout) -----
    if (s.injected === "comms") {
      if (s.t - s.lastRxT > LIMIT.commsTimeoutS && s.phase === PHASE.TRANSFER) {
        trip(s, "F-COM-05", "ACAN communication timeout");
        s.injected = null;
      }
    } else {
      s.lastRxT = s.t;
    }

    // ----- physics targets -----
    let vTarget = 0;
    const evPack = modelRef.current.packV;
    if (s.phase === PHASE.PRECHARGE)
      vTarget = (isDC ? 800 : evPack) * Math.min(1, s.phaseT / PHASE_DUR.PRECHARGE);
    else if (s.phase === PHASE.TRANSFER) {
      vTarget = isDC ? 800 : evPack;
      // real 800V rail sags slightly under current surge (finite source impedance),
      // then the regulator + storage pull it back — visible dips during transients
      if (isDC) vTarget = 800 - 0.03 * (s.iBus - 0.86 * maxP / 800);
    }
    else if (s.phase === PHASE.SHUTDOWN || s.phase === PHASE.FAULT || s.phase === PHASE.COMPLETE) vTarget = 0;

    // injected overvoltage spike
    if (s.injected === "ov" && s.phase === PHASE.TRANSFER) vTarget = 935;

    // voltage slew
    const vSlew = s.phase === PHASE.FAULT ? 2600 : 900;
    s.vBus += clamp(vTarget - s.vBus, -vSlew * DT, vSlew * DT);
    if (s.phase === PHASE.TRANSFER && s.injected !== "ov")
      s.vBus += (Math.random() - 0.5) * 2.2; // ripple

    // ----- current model -----
    let iTarget = 0;
    if (s.phase === PHASE.TRANSFER && s.contactor) {
      if (isDC) {
        // synchronized GPU load transients (training step boundaries):
        // periodic surges/dips the storage buffers, so current visibly moves
        if (s.txT === undefined) { s.txT = 2.5; s.txPulse = 0; s.txMag = 0; s.txDur0 = 1; }
        s.txT -= DT;
        if (s.txT <= 0) {
          s.txMag = (Math.random() < 0.5 ? -1 : 1) * (14 + Math.random() * 10); // ±14-24% swing
          s.txDur0 = 0.9 + Math.random() * 0.7;
          s.txPulse = s.txDur0;
          s.txT = 3 + Math.random() * 3;
        }
        let txOff = 0;
        if (s.txPulse > 0) {
          txOff = s.txMag * Math.sin(Math.PI * (1 - s.txPulse / s.txDur0)); // smooth in/out
          s.txPulse -= DT;
        }
        s.buffering = Math.abs(txOff) > 4;
        const set = clamp(86 + txOff, 30, 100); // capped under the OC trip point
        s.load += clamp(set - s.load, -160 * DT, 170 * DT);
        s.load += (Math.random() - 0.5) * 0.6;
        const p = (s.load / 100) * maxP;
        iTarget = p / Math.max(1, s.vBus);
      } else {
        // CC-CV lithium curve, parameterised by the selected battery model.
        // CC holds near iMax, then a progressive CV taper begins ~72% SoC:
        // current (and power) fall smoothly — ~90% of iMax at 80%, ~60% at 90%,
        // trickle near 100% (why the last 10% is slow).
        const m = modelRef.current;
        const iMax = m.iMax;
        let i = iMax;
        if (s.soc >= 72) {
          const x = (s.soc - 72) / 28; // 0 at 72% -> 1 at 100%
          i = iMax * Math.max(0.06, 1 - x * x * 0.94);
        }
        iTarget = i * Math.min(1, s.phaseT / 1.0);
        s.soc = Math.min(100, s.soc + (iTarget / iMax) * (100 / m.fullSec) * DT);
        if (s.soc >= 99.5) {
          s.soc = 100;
          pushCan(s, "stop", [0x02], "tx");
          s.phase = PHASE.COMPLETE;
          s.phaseT = 0;
          s.contactor = false;
        }
      }
    }
    // injected overcurrent spike
    if (s.injected === "oc" && s.phase === PHASE.TRANSFER)
      iTarget = isDC ? 1500 : 780;

    const iSlew = s.phase === PHASE.FAULT || !s.contactor ? 4200 : 1400;
    s.iBus += clamp(iTarget - s.iBus, -iSlew * DT, iSlew * DT);
    if (!s.contactor) s.iBus = Math.max(0, s.iBus - 3000 * DT);

    // ----- insulation monitor -----
    if (s.injected === "iso") {
      s.iso += clamp(0.03 - s.iso, -6000 * DT, 0); // collapse fast
    } else if (s.phase === PHASE.INSULATION || s.phase === PHASE.TRANSFER) {
      s.iso += clamp(540 - s.iso, -800 * DT, 800 * DT) + (Math.random() - 0.5) * 4;
    } else {
      s.iso += clamp(999 - s.iso, -400 * DT, 400 * DT);
    }

    // ----- thermal -----
    // heat scaled so a full-power charge plateaus ~60 C (safely under the 85 C
    // limit); the injected "ot" fault overrides cooling to force a runaway.
    const pNow = (s.vBus * s.iBus) / 1000; // kW
    const heat = (Math.abs(s.vBus * s.iBus) / maxP) * 10;
    let cool = (s.temp - 25) * 0.28;
    if (s.injected === "ot") { cool -= 130; } // runaway
    s.temp += (heat - cool) * DT;
    s.temp = Math.max(25, s.temp);

    // ----- efficiency -----
    const loadFrac = Math.abs(s.vBus * s.iBus) / maxP;
    s.eff =
      s.phase === PHASE.TRANSFER
        ? clamp(effBase - loadFrac * 1.6 - Math.max(0, s.temp - 60) * 0.05, 90, 99.4)
        : 0;

    // ----- protection logic (checked every tick) -----
    if (s.phase !== PHASE.FAULT && s.phase !== PHASE.IDLE) {
      if (s.iso < LIMIT.isoMinMohm)
        trip(s, "F-ISO-01", "Insulation resistance low · IMD");
      else if (s.vBus > LIMIT.vBusMax)
        trip(s, "F-OV-02", "DC bus overvoltage");
      else if (s.iBus > (isDC ? 1320 : 680))
        trip(s, "F-OC-03", "Overcurrent · contactor protection");
      else if (s.temp > LIMIT.tempMaxC)
        trip(s, "F-OT-04", "Power module over-temperature");
    }

    // ----- periodic CAN traffic -----
    if (s.phase === PHASE.HANDSHAKE && Math.abs(s.phaseT - 0.3) < DT / 2) {
      pushCan(s, "hsA", [0x11, 0x08, 0x00, 0x00], "tx");
      pushCan(s, "hsB", [0x11, 0x08, (modelRef.current.packV >> 8) & 0xff, modelRef.current.packV & 0xff], "rx");
    }
    if (s.phase === PHASE.INSULATION && Math.abs(s.phaseT - 1.2) < DT / 2) {
      const v = Math.round(Math.min(s.iso, 999));
      pushCan(s, "isoSt", [(v >> 8) & 0xff, v & 0xff, 0x01], "rx");
    }
    if (s.phase === PHASE.TRANSFER) {
      s._msgAcc = (s._msgAcc || 0) + DT;
      if (s._msgAcc >= 0.6) {
        s._msgAcc = 0;
        const vh = Math.round(s.vBus);
        const ih = Math.round(s.iBus / 4);
        pushCan(s, "req", [(vh >> 8) & 0xff, vh & 0xff, (ih >> 8) & 0xff, ih & 0xff], "rx");
        if (s.injected !== "comms") {
          const pct = Math.round(isDC ? s.load : s.soc);
          pushCan(s, "stat", [(vh >> 8) & 0xff, vh & 0xff, ih & 0xff, pct], "tx");
          s.lastRxT = s.t;
        }
      }
    }

    // ----- history for trace -----
    s.hist.push({ t: +s.t.toFixed(1), v: +s.vBus.toFixed(1), i: +s.iBus.toFixed(1) });
    if (s.hist.length > 90) s.hist.shift();

    setTick((x) => (x + 1) % 100000);
  }, []);

  useEffect(() => {
    const id = setInterval(step, DT * 1000);
    return () => clearInterval(id);
  }, [step]);

  // ---- optional live link to the Python bench (bench.py --ws) ----
  const [live, setLive] = useState(null);
  const [benchUp, setBenchUp] = useState(false);
  useEffect(() => {
    let ws, retry;
    const connect = () => {
      try {
        ws = new WebSocket("ws://localhost:8765");
        ws.onopen = () => { liveRef.current = true; setBenchUp(true); };
        ws.onmessage = (e) => {
          try {
            const msg = JSON.parse(e.data);
            setLive(msg);
            if (msg.frames && msg.frames.length) {
              const s = sim.current;
              for (const f of msg.frames) {
                s.can.unshift({ k: s.canId++, t: f.t, proto: f.proto,
                                id: f.id, name: f.name, data: f.data, dir: f.dir });
              }
              if (s.can.length > 60) s.can.length = 60;
            }
          } catch {}
        };
        ws.onclose = () => { liveRef.current = false; setBenchUp(false); retry = setTimeout(connect, 2000); };
        ws.onerror = () => { try { ws.close(); } catch {} };
      } catch { retry = setTimeout(connect, 2000); }
    };
    connect();
    return () => { clearTimeout(retry); try { ws && ws.close(); } catch {} };
  }, []);

  // ---- controls ----
  const s = sim.current;
  const start = () => {
    if (s.phase !== PHASE.IDLE) return;
    s.phase = PHASE.HANDSHAKE;
    s.phaseT = 0;
    s.lastRxT = s.t;
    pushCan(s, "hsA", [0x01, 0x00, 0x00, 0x00], "tx");
  };
  const stop = () => {
    if (s.phase === PHASE.IDLE) return;
    s.phase = PHASE.SHUTDOWN;
    s.phaseT = 0;
    s.contactor = false;
    s.injected = null;
    pushCan(s, "stop", [0x01], "tx");
  };
  const reset = () => {
    sim.current = fresh(s.mode);
    flash.current = 0;
  };
  const setMode = (m) => {
    sim.current = fresh(m);
    flash.current = 0;
  };
  const inject = (key) => {
    if (s.phase === PHASE.FAULT || s.phase === PHASE.IDLE) return;
    s.injected = key;
  };

  const isDC = s.mode === "dc";
  const accent = isDC ? C.dcAccent : C.evAccent;
  const faulted = s.phase === PHASE.FAULT;
  const showFlash = flash.current > 0 && !rmMotion.current;
  const maxP = isDC ? aiChip.rackKW : 240; // kW
  const pKW = (s.vBus * s.iBus) / 1000;

  return (
    <div style={styles.root(faulted, showFlash)}>
      <FontsAndKeyframes />

      {/* ---------- header ---------- */}
      <header style={styles.header}>
        <div style={{ display: "flex", alignItems: "baseline", gap: 14, flexWrap: "wrap" }}>
          <div style={styles.brand}>
            VOLT<span style={{ color: accent }}>BRIDGE</span>
          </div>
          <div style={styles.brandSub}>
            800 VDC · HARDWARE-IN-THE-LOOP BENCH
          </div>
        </div>

        <div style={{ display: "flex", alignItems: "center", gap: 16, flexWrap: "wrap" }}>
          <div style={{ ...styles.stdTag, borderColor: benchUp ? C.ok : C.line, color: benchUp ? C.ok : C.muted }}>
            {benchUp ? "● LIVE · python bench" : "○ bench offline"}
          </div>
          <div style={styles.stdTag}>{isDC ? "800VDC AI RACK · PMBus + Modbus" : "IS 17017 · CCS2 / ISO 15118 · ACAN"}</div>
          <ModeToggle mode={s.mode} onChange={setMode} disabled={s.phase !== PHASE.IDLE} />
          {!isDC ? (
            <ModelSelect model={evModel} onChange={selectModel} disabled={s.phase !== PHASE.IDLE} />
          ) : (
            <ChipSelect chip={aiChip} onChange={selectChip} disabled={s.phase !== PHASE.IDLE} />
          )}
          <StatePill phase={s.phase} accent={accent} />
        </div>
      </header>

      {/* ---------- scrollable content ---------- */}
      <main style={styles.main}>
      {/* ---------- main grid ---------- */}
      <div className="vb-grid" style={styles.grid}>
        {/* left telemetry column */}
        <section style={styles.col}>
          <Gauge
            label="DC BUS VOLTAGE"
            value={s.vBus}
            max={1000}
            unit="V"
            color={C.volt}
            warnAt={LIMIT.vBusMax}
          />
          <Gauge
            label="BUS CURRENT"
            value={s.iBus}
            max={isDC ? 1400 : 720}
            unit="A"
            color={C.amp}
            warnAt={isDC ? 1320 : 680}
          />
          <div style={styles.tileRow}>
            <StatTile label="POWER" value={fmt(pKW, pKW >= 100 ? 0 : 1)} unit="kW" color={C.power} sub={`of ${maxP} kW`} />
            <StatTile
              label="MODULE TEMP"
              value={fmt(s.temp, 1)}
              unit="°C"
              color={s.temp > 70 ? C.warn : C.ok}
              sub={`limit ${LIMIT.tempMaxC}`}
            />
          </div>
          <div style={styles.tileRow}>
            <StatTile
              label="EFFICIENCY"
              value={s.eff ? fmt(s.eff, 1) : "—"}
              unit={s.eff ? "%" : ""}
              color={C.ok}
              sub="end-to-end"
            />
            <StatTile
              label="INSULATION"
              value={s.iso > 900 ? ">900" : fmt(s.iso, s.iso < 1 ? 3 : 0)}
              unit="MΩ"
              color={s.iso < 1 ? C.fault : C.ok}
              sub={`min ${LIMIT.isoMinMohm}`}
            />
          </div>
          <SocBar isDC={isDC} soc={s.soc} load={s.load} accent={accent} model={evModel} />
          {!isDC && (
            <div style={{ display: "flex", alignItems: "center", gap: 8, fontFamily: "IBM Plex Mono, monospace", fontSize: 11, color: C.faint, marginTop: -4 }}>
              <CarIcon color={evModel.color} body={evModel.body} size={30} />
              <span><span style={{ color: C.muted }}>{evModel.name}</span> · {evModel.kwh} kWh · {evModel.packV}V · {evKW(evModel)} kW</span>
            </div>
          )}
          <ProtectionPanel phase={s.phase} faultCode={s.faultCode} isDC={isDC} />
        </section>

        {/* center: trace + contactor */}
        <section style={styles.col}>
          <div style={styles.panel}>
            <PanelHead>LIVE TRACE · V / I</PanelHead>
            <div style={{ height: 208, marginTop: 6 }}>
              <ResponsiveContainer width="100%" height="100%">
                <LineChart data={s.hist} margin={{ top: 8, right: 8, left: -6, bottom: 0 }}>
                  <XAxis dataKey="t" hide />
                  <YAxis yAxisId="v" domain={[0, 1000]} width={34} tick={{ fill: C.volt, fontSize: 10, fontFamily: "IBM Plex Mono, monospace" }} axisLine={{ stroke: C.line }} tickLine={false} />
                  <YAxis yAxisId="i" orientation="right" domain={[0, isDC ? 1400 : 720]} width={40} tick={{ fill: C.amp, fontSize: 10, fontFamily: "IBM Plex Mono, monospace" }} axisLine={{ stroke: C.line }} tickLine={false} />
                  <ReferenceLine yAxisId="v" y={800} stroke={C.line} strokeDasharray="3 4" />
                  <Line yAxisId="v" type="monotone" dataKey="v" stroke={C.volt} strokeWidth={2} dot={false} isAnimationActive={false} />
                  <Line yAxisId="i" type="monotone" dataKey="i" stroke={C.amp} strokeWidth={2} dot={false} isAnimationActive={false} />
                </LineChart>
              </ResponsiveContainer>
            </div>
            <div style={styles.legendRow}>
              <Legend color={C.volt} text="Bus voltage (V)" />
              <Legend color={C.amp} text="Bus current (A)" />
              <span style={{ marginLeft: "auto", color: C.faint, fontFamily: "IBM Plex Mono, monospace", fontSize: 11 }}>
                t = {s.t.toFixed(1)} s
              </span>
            </div>
          </div>

          <div style={styles.panel}>
            <PanelHead>HV INTERLOCK</PanelHead>
            <Contactor closed={s.contactor} faulted={faulted} accent={accent} reduced={rmMotion.current} />
          </div>

          {isDC && (
            <DataCenterPanel
              live={live && live.mode === "dc" ? live : null}
              loadPct={s.load}
              buffering={s.buffering}
              chip={aiChip}
              running={s.phase === "TRANSFER"}
              reduced={rmMotion.current}
            />
          )}
        </section>

        {/* right: CAN log */}
        <section style={styles.col}>
          <div style={{ ...styles.panel, display: "flex", flexDirection: "column", minHeight: 0 }}>
            <PanelHead>
              {isDC ? "RACK BUS · PMBus + Modbus" : "INTERNAL ACAN CONTROL BUS"}
              <span style={{ marginLeft: "auto", fontSize: 10, color: s.injected === "comms" ? C.fault : C.ok, fontFamily: "IBM Plex Mono, monospace" }}>
                {s.injected === "comms" ? "● LINK LOST" : "● LINK UP"}
              </span>
            </PanelHead>
            <div style={styles.busCaption}>
              {benchUp
                ? <span style={{ color: C.ok, fontWeight: 600 }}>● LIVE — real frames streamed from the Python bench</span>
                : (isDC
                    ? "PMBus on the power components (rectifier, DC-DC) · Modbus-RTU on the battery / BESS"
                    : "External charging link: CCS2 / ISO 15118 (PLC) · shown below: on-board control & protection")}
            </div>
            <CanLog rows={s.can} />
          </div>
        </section>
      </div>
      </main>

      {/* ---------- state machine strip ---------- */}
      <StateStrip phase={s.phase} accent={accent} />

      {/* ---------- fault banner ---------- */}
      {faulted && (
        <div style={styles.faultBanner}>
          <span style={styles.faultCode}>{s.faultCode}</span>
          <span style={styles.faultName}>{s.faultName}</span>
          <span style={styles.faultMeta}>
            Contactor open · current interrupted in &lt;200 ms · press RESET
          </span>
        </div>
      )}

      {/* ---------- charge-complete banner ---------- */}
      {s.phase === PHASE.COMPLETE && (
        <div style={styles.completeBanner}>
          <span style={styles.completeCheck}>✓</span>
          <span style={styles.completeName}>Charge complete — battery at 100%</span>
          <span style={styles.faultMeta}>
            Contactor open · session ended normally · press RESET to run again
          </span>
        </div>
      )}

      {/* ---------- controls ---------- */}
      <footer style={styles.footer}>
        <div style={styles.ctrlGroup}>
          <Ctrl label="START" onClick={start} tone="go" disabled={s.phase !== PHASE.IDLE} />
          <Ctrl label="STOP" onClick={stop} tone="neutral" disabled={s.phase === PHASE.IDLE || faulted || s.phase === PHASE.COMPLETE} />
          <Ctrl label="RESET" onClick={reset} tone="neutral" />
        </div>

        <div style={styles.faultGroup}>
          <span style={styles.faultGroupLabel}>INJECT FAULT</span>
          <FaultBtn label="Insulation" onClick={() => inject("iso")} armed={s.injected === "iso"} disabled={disabledInject(s)} />
          <FaultBtn label="Overvoltage" onClick={() => inject("ov")} armed={s.injected === "ov"} disabled={disabledInject(s)} />
          <FaultBtn label="Overcurrent" onClick={() => inject("oc")} armed={s.injected === "oc"} disabled={disabledInject(s)} />
          <FaultBtn label="Over-temp" onClick={() => inject("ot")} armed={s.injected === "ot"} disabled={disabledInject(s)} />
          <FaultBtn label="Comms loss" onClick={() => inject("comms")} armed={s.injected === "comms"} disabled={disabledInject(s)} />
        </div>
      </footer>
    </div>
  );
}

const disabledInject = (s) => s.phase === PHASE.IDLE || s.phase === PHASE.FAULT || s.phase === PHASE.COMPLETE;

/* ============================= sub-components ============================= */

function ModeToggle({ mode, onChange, disabled }) {
  const opt = (m, label) => {
    const active = mode === m;
    return (
      <button
        onClick={() => !disabled && onChange(m)}
        disabled={disabled}
        style={{
          ...styles.toggleBtn,
          background: active ? (m === "dc" ? C.dcAccent : C.evAccent) : "transparent",
          color: active ? "#06121A" : disabled ? C.faint : C.muted,
          cursor: disabled ? "not-allowed" : "pointer",
        }}
      >
        {label}
      </button>
    );
  };
  return (
    <div style={styles.toggleWrap} title={disabled ? "Return to Standby to switch domain" : ""}>
      {opt("ev", "EV FAST-CHARGE")}
      {opt("dc", "AI DATA CENTER")}
    </div>
  );
}

function CarIcon({ color, body = "coupe", size = 24 }) {
  // generic side-view silhouettes (no brand marks), tinted per model
  const roof = {
    coupe: "M20 12 Q26 6 34 6 L42 6 Q47 6 50 12 Z",
    sedan: "M18 12 L23 6 L41 6 L47 12 Z",
    suv:   "M18 12 L22 5 L44 5 L48 12 Z",
  }[body];
  const glass = {
    coupe: "M23 11 L33 8 L33 11 Z M35 8 L44 8 L47 11 L35 11 Z",
    sedan: "M22 11 L25 8 L31 8 L31 11 Z M33 8 L40 8 L44 11 L33 11 Z",
    suv:   "M22 11 L24 7 L31 7 L31 11 Z M33 7 L42 7 L45 11 L33 11 Z",
  }[body];
  return (
    <svg width={size} height={size * 0.5} viewBox="0 0 64 32" style={{ flexShrink: 0 }}>
      <path d={roof} fill={color} opacity="0.55" />
      <path d="M4 20 Q5 13 14 12 L50 12 Q58 13 60 18 L60 20 Q60 23 57 23 L7 23 Q4 23 4 20 Z" fill={color} />
      <path d={glass} fill="rgba(255,255,255,0.30)" />
      <circle cx="18" cy="23" r="5" fill="#0b0f14" stroke={color} strokeWidth="1.6" />
      <circle cx="46" cy="23" r="5" fill="#0b0f14" stroke={color} strokeWidth="1.6" />
    </svg>
  );
}

function ModelSelect({ model, onChange, disabled }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open]);
  const groups = ["German · 800V", "Italian · 800V", "Korean · 800V", "USA · 400V", "German · 400V", "Japanese · 400V"];
  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        onClick={() => !disabled && setOpen((o) => !o)}
        disabled={disabled}
        title={disabled ? "Return to Standby to change vehicle" : "Select vehicle / battery"}
        style={{
          display: "flex", alignItems: "center", gap: 8,
          fontFamily: "IBM Plex Mono, monospace", fontSize: 12,
          background: C.panel, color: disabled ? C.faint : C.text,
          border: `1px solid ${open ? C.evAccent : C.line}`, borderRadius: 8,
          padding: "6px 10px", cursor: disabled ? "not-allowed" : "pointer",
        }}
      >
        <CarIcon color={model.color} body={model.body} size={26} />
        <span style={{ fontWeight: 700 }}>{model.name}</span>
        <span style={{ color: model.packV >= 800 ? C.evAccent : C.volt }}>{model.packV}V</span>
        <span style={{ color: C.faint }}>▾</span>
      </button>
      {open && (
        <div style={{
          position: "absolute", top: "112%", right: 0, zIndex: 60, width: 300,
          maxHeight: 340, overflowY: "auto", overscrollBehavior: "contain",
          background: C.panel, border: `1px solid ${C.line}`, borderRadius: 10,
          boxShadow: "0 12px 30px rgba(5,7,11,0.6)", padding: 6,
        }}>
          {groups.map((g) => (
            <div key={g}>
              <div style={{ fontFamily: "IBM Plex Mono, monospace", fontSize: 10, letterSpacing: 1, color: C.faint, textTransform: "uppercase", padding: "8px 8px 3px" }}>{g}</div>
              {EV_MODELS.filter((m) => m.region === g).map((m) => (
                <button key={m.id} onClick={() => { onChange(m); setOpen(false); }}
                  style={{
                    display: "flex", alignItems: "center", gap: 9, width: "100%", textAlign: "left",
                    background: m.id === model.id ? "rgba(45,212,167,0.10)" : "transparent",
                    border: "none", borderRadius: 7, padding: "6px 8px", cursor: "pointer",
                    color: C.text, fontFamily: "IBM Plex Mono, monospace", fontSize: 12,
                  }}>
                  <CarIcon color={m.color} body={m.body} size={26} />
                  <span style={{ flex: 1, fontWeight: m.id === model.id ? 700 : 400 }}>{m.name}</span>
                  <span style={{ color: C.faint }}>{m.kwh}kWh · {evKW(m)}kW</span>
                </button>
              ))}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

function ChipIcon({ color, colorB, size = 24 }) {
  return (
    <svg width={size} height={size} viewBox="0 0 32 32" style={{ flexShrink: 0 }}>
      <rect x="9" y="9" width="14" height="14" rx="2" fill={color} opacity="0.9" />
      {colorB && <rect x="16" y="9" width="7" height="14" rx="0" fill={colorB} opacity="0.9" />}
      <rect x="12.5" y="12.5" width="7" height="7" rx="1" fill="#0b0f14" opacity="0.55" />
      {[11, 16, 21].map((p) => (
        <g key={p}>
          <rect x={p - 0.6} y="4" width="1.2" height="4" fill={colorB && p > 16 ? colorB : color} />
          <rect x={p - 0.6} y="24" width="1.2" height="4" fill={colorB && p > 16 ? colorB : color} />
          <rect x="4" y={p - 0.6} width="4" height="1.2" fill={color} />
          <rect x="24" y={p - 0.6} width="4" height="1.2" fill={colorB || color} />
        </g>
      ))}
    </svg>
  );
}

function ChipSelect({ chip, onChange, disabled }) {
  const [open, setOpen] = useState(false);
  const ref = useRef(null);
  useEffect(() => {
    if (!open) return;
    const h = (e) => { if (ref.current && !ref.current.contains(e.target)) setOpen(false); };
    document.addEventListener("mousedown", h);
    return () => document.removeEventListener("mousedown", h);
  }, [open]);
  const vendors = ["NVIDIA", "AMD", "Google", "Amazon", "Huawei", "Mixed"];
  const shortName = (n) => n.replace(/^(NVIDIA|AMD|Google|AWS|Huawei) /, "");
  return (
    <div ref={ref} style={{ position: "relative" }}>
      <button
        onClick={() => !disabled && setOpen((o) => !o)}
        disabled={disabled}
        title={disabled ? "Return to Standby to change accelerator" : "Select AI accelerator"}
        style={{
          display: "flex", alignItems: "center", gap: 8,
          fontFamily: "IBM Plex Mono, monospace", fontSize: 12,
          background: C.panel, color: disabled ? C.faint : C.text,
          border: `1px solid ${open ? C.dcAccent : C.line}`, borderRadius: 8,
          padding: "6px 10px", cursor: disabled ? "not-allowed" : "pointer",
        }}
      >
        <ChipIcon color={chip.color} colorB={chip.colorB} size={20} />
        <span style={{ fontWeight: 700 }}>{shortName(chip.name)}</span>
        <span style={{ color: chip.dense ? C.dcAccent : C.faint }}>{chip.rackKW}kW</span>
        <span style={{ color: C.faint }}>▾</span>
      </button>
      {open && (
        <div style={{
          position: "absolute", top: "112%", right: 0, zIndex: 60, width: 320,
          maxHeight: 360, overflowY: "auto", overscrollBehavior: "contain",
          background: C.panel, border: `1px solid ${C.line}`, borderRadius: 10,
          boxShadow: "0 12px 30px rgba(5,7,11,0.6)", padding: 6,
        }}>
          {vendors.map((v) => (
            <div key={v}>
              <div style={{ fontFamily: "IBM Plex Mono, monospace", fontSize: 10, letterSpacing: 1, color: C.faint, textTransform: "uppercase", padding: "8px 8px 3px" }}>{v}</div>
              {AI_CHIPS.filter((c) => c.vendor === v).map((c) => (
                <button key={c.id} onClick={() => { onChange(c); setOpen(false); }}
                  style={{
                    display: "flex", alignItems: "center", gap: 9, width: "100%", textAlign: "left",
                    background: c.id === chip.id ? "rgba(242,177,56,0.12)" : "transparent",
                    border: "none", borderRadius: 7, padding: "6px 8px", cursor: "pointer",
                    color: C.text, fontFamily: "IBM Plex Mono, monospace", fontSize: 12,
                  }}>
                  <ChipIcon color={c.color} colorB={c.colorB} size={20} />
                  <span style={{ flex: 1, fontWeight: c.id === chip.id ? 700 : 400 }}>{shortName(c.name)}</span>
                  <span style={{ color: c.dense ? C.dcAccent : C.faint }}>{c.rackKW}kW{c.dense ? " · 800V" : ""}</span>
                </button>
              ))}
            </div>
          ))}
          <div style={{ fontFamily: "IBM Plex Mono, monospace", fontSize: 9.5, color: C.faint, padding: "6px 8px 2px", lineHeight: 1.5 }}>
            Power approximate · (est.) = unpublished · dense racks drive the 800VDC move
          </div>
        </div>
      )}
    </div>
  );
}

function ProtectionPanel({ phase, faultCode, isDC }) {
  const active = phase !== PHASE.IDLE;
  const rows = [
    ["F-ISO-01", "Insulation", "\u2265 0.1 M\u03A9"],
    ["F-OV-02", "DC bus", "\u2264 900 V"],
    ["F-OC-03", "Overcurrent", isDC ? "\u2264 1320 A" : "\u2264 680 A"],
    ["F-OT-04", "Module temp", "\u2264 85 \u00B0C"],
    ["F-COM-05", "ACAN comms", "\u2264 1.2 s"],
  ];
  return (
    <div style={{ ...styles.panel, minHeight: 0, display: "flex", flexDirection: "column" }}>
      <PanelHead>PROTECTION LIMITS</PanelHead>
      <div style={{ display: "flex", flexDirection: "column", gap: 9, marginTop: 10 }}>
        {rows.map(([code, name, lim]) => {
          const tripped = faultCode === code;
          const col = tripped ? C.fault : active ? C.ok : C.faint;
          return (
            <div key={code} style={{ display: "flex", alignItems: "center", gap: 10, fontFamily: "IBM Plex Mono, monospace", fontSize: 12 }}>
              <span style={{ width: 8, height: 8, borderRadius: "50%", background: col, boxShadow: tripped ? `0 0 8px ${col}` : "none", flexShrink: 0 }} />
              <span style={{ color: C.faint, width: 78, flexShrink: 0 }}>{code}</span>
              <span style={{ color: tripped ? C.fault : C.text, flex: 1 }}>{name}</span>
              <span style={{ color: tripped ? C.fault : C.muted }}>{lim}</span>
            </div>
          );
        })}
      </div>
      <div style={{ marginTop: "auto", paddingTop: 12, color: C.faint, fontFamily: "IBM Plex Mono, monospace", fontSize: 10.5 }}>
        {active ? "monitoring \u00B7 checked every cycle" : "armed on START"}
      </div>
    </div>
  );
}

function StatePill({ phase, accent }) {
  const faulted = phase === PHASE.FAULT;
  const complete = phase === PHASE.COMPLETE;
  const running = phase !== PHASE.IDLE && !faulted && !complete;
  const color = faulted ? C.fault : complete ? C.ok : running ? accent : C.faint;
  return (
    <div style={{ ...styles.statePill, borderColor: color, color }}>
      <span style={{ ...styles.dot, background: color, boxShadow: `0 0 10px ${color}` }} />
      {PHASE_LABEL[phase]}
    </div>
  );
}

function Gauge({ label, value, max, unit, color, warnAt }) {
  const f = clamp(value / max, 0, 1);
  const START = -120,
    SWEEP = 240;
  const R = 78,
    CX = 100,
    CY = 96;
  const pts = 48;
  const arc = (frac) => {
    let d = "";
    for (let k = 0; k <= pts; k++) {
      const ang = START + (SWEEP * frac * k) / pts;
      const [x, y] = polar(CX, CY, R, ang);
      d += (k === 0 ? "M" : "L") + x.toFixed(1) + " " + y.toFixed(1) + " ";
    }
    return d;
  };
  const warnFrac = warnAt ? clamp(warnAt / max, 0, 1) : 1;
  const over = warnAt && value > warnAt;
  const [nx, ny] = polar(CX, CY, R, START + SWEEP * f);
  return (
    <div style={styles.panel}>
      <PanelHead>{label}</PanelHead>
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <svg viewBox="0 0 200 118" width="132" height="78" style={{ flexShrink: 0 }}>
          <path d={arc(1)} fill="none" stroke={C.lineSoft} strokeWidth={10} strokeLinecap="round" />
          {warnAt && (
            <path
              d={(() => {
                let d = "";
                for (let k = 0; k <= pts; k++) {
                  const fr = warnFrac + ((1 - warnFrac) * k) / pts;
                  const ang = START + SWEEP * fr;
                  const [x, y] = polar(CX, CY, R, ang);
                  d += (k === 0 ? "M" : "L") + x.toFixed(1) + " " + y.toFixed(1) + " ";
                }
                return d;
              })()}
              fill="none"
              stroke={C.fault}
              strokeWidth={10}
              strokeLinecap="round"
              opacity={0.35}
            />
          )}
          <path d={arc(f)} fill="none" stroke={over ? C.fault : color} strokeWidth={10} strokeLinecap="round" />
          <circle cx={nx} cy={ny} r={5} fill={over ? C.fault : color} />
        </svg>
        <div style={{ marginLeft: "auto", textAlign: "right" }}>
          <div style={{ ...styles.gaugeVal, color: over ? C.fault : C.text }}>
            {fmt(value, value >= 100 ? 0 : 1)}
          </div>
          <div style={styles.gaugeUnit}>{unit}</div>
        </div>
      </div>
    </div>
  );
}

function StatTile({ label, value, unit, color, sub }) {
  return (
    <div style={{ ...styles.panel, flex: 1, padding: "10px 12px" }}>
      <div style={styles.tileLabel}>{label}</div>
      <div style={{ display: "flex", alignItems: "baseline", gap: 4 }}>
        <span style={{ ...styles.tileVal, color }}>{value}</span>
        <span style={styles.tileUnit}>{unit}</span>
      </div>
      <div style={styles.tileSub}>{sub}</div>
    </div>
  );
}

function SocBar({ isDC, soc, load, accent, model }) {
  const pct = isDC ? load : soc;
  const showCar = !isDC && model;
  return (
    <div style={{ ...styles.panel, padding: "12px 14px" }}>
      <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", marginBottom: 8 }}>
        <span style={styles.tileLabel}>{isDC ? "IT LOAD" : "STATE OF CHARGE"}</span>
        <span style={{ fontFamily: "IBM Plex Mono, monospace", color: accent, fontSize: 13, fontWeight: 600 }}>
          {fmt(pct, 0)}%
        </span>
      </div>
      <div style={{ position: "relative", marginTop: showCar ? 14 : 0 }}>
        <div style={styles.barTrack}>
          <div style={{ ...styles.barFill, width: `${clamp(pct, 0, 100)}%`, background: accent }} />
        </div>
        {showCar && (
          <div style={{
            position: "absolute", top: -13, left: `${clamp(pct, 0, 100)}%`,
            transform: "translateX(-50%)", transition: "left .25s ease", pointerEvents: "none",
          }}>
            <CarIcon color={model.color} body={model.body} size={28} />
          </div>
        )}
      </div>
    </div>
  );
}

function Contactor({ closed, faulted, accent, reduced }) {
  const color = faulted ? C.fault : closed ? accent : C.faint;
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 18, padding: "8px 4px 2px" }}>
      <svg viewBox="0 0 220 90" width="100%" height="88" style={{ maxWidth: 340 }}>
        {/* rails */}
        <line x1="6" y1="45" x2="70" y2="45" stroke={color} strokeWidth={4} strokeLinecap="round" />
        <line x1="150" y1="45" x2="214" y2="45" stroke={color} strokeWidth={4} strokeLinecap="round" />
        <circle cx="70" cy="45" r="6" fill={color} />
        <circle cx="150" cy="45" r="6" fill={color} />
        {/* moving blade */}
        <line
          x1="70"
          y1="45"
          x2="150"
          y2={closed ? 45 : 14}
          stroke={color}
          strokeWidth={5}
          strokeLinecap="round"
          style={{ transition: reduced ? "none" : "all .28s cubic-bezier(.5,1.4,.5,1)" }}
        />
        {/* arc-flash cue on open */}
        {!closed && !reduced && (
          <circle cx="150" cy="45" r="10" fill="none" stroke={C.fault} strokeWidth="1.5" opacity="0.6">
            <animate attributeName="r" values="6;16;6" dur="0.9s" repeatCount="indefinite" />
            <animate attributeName="opacity" values="0.6;0;0.6" dur="0.9s" repeatCount="indefinite" />
          </circle>
        )}
        {closed &&
          !reduced &&
          [0, 1, 2].map((i) => (
            <circle key={i} r="2.5" fill={accent}>
              <animateMotion dur="1.1s" begin={`${i * 0.36}s`} repeatCount="indefinite" path="M6 45 L214 45" />
            </circle>
          ))}
      </svg>
      <div>
        <div style={{ ...styles.tileLabel }}>MAIN CONTACTOR</div>
        <div style={{ fontFamily: "Chakra Petch, sans-serif", fontSize: 20, fontWeight: 700, color }}>
          {faulted ? "TRIPPED" : closed ? "CLOSED" : "OPEN"}
        </div>
      </div>
    </div>
  );
}

function DataCenterPanel({ live, loadPct, buffering: buffProp, chip, running, reduced }) {
  const N = live?.n_trays || 8;
  const rackNom = chip?.rackKW ?? 132 * 8; // nominal rack power (kW)
  const pTray = rackNom / N; // per-tray nominal (kW)
  // stable per-tray spread so the 8 trays differ (not a uniform block) but still
  // scale together with load; during a transient they rise and flush warm.
  const hash = (i) => { const x = Math.sin((i + 1) * 99.13) * 43758.5; return x - Math.floor(x); };
  const trays = live?.trays || Array.from({ length: N }, (_, i) =>
    running ? (loadPct / 100) * pTray * (0.82 + 0.36 * hash(i)) : 0);
  const rack = live?.rack_power_kw ?? trays.reduce((a, b) => a + b, 0);
  const grid = live?.grid_power_kw ?? rack;
  const e2e = live?.e2e_eff ?? (running ? 95.4 : 0);
  const base = live?.baseline_eff ?? 90.5;
  const gain = live?.eff_gain ?? +(e2e - base).toFixed(2);
  const soc = live?.storage_soc ?? 80;
  // storage buffers the transient portion (load deviation from the ~86% nominal)
  const nomLoad = 86;
  const spLocal = running ? ((loadPct - nomLoad) / 100) * rackNom : 0; // kW, + discharge / - recharge
  const sp = live?.storage_power ?? spLocal;
  const buffering = live?.buffering ?? (buffProp ?? Math.abs(spLocal) > 30);
  const maxTray = pTray * 1.3;

  // load-reactive tray color: green (light) -> amber (normal) -> orange (surge).
  // for a mixed rack, colour trays by vendor instead (first half A, second half B).
  const trayColor = (kw) => {
    const f = kw / pTray;
    if (f > 1.0) return C.warn;
    if (f < 0.72) return C.ok;
    return C.dcAccent;
  };
  const trayFill = (kw, i) => (chip?.mix ? (i < N / 2 ? chip.color : chip.colorB) : trayColor(kw));

  const stage = (label, sub, on) => (
    <div style={{ flex: 1, textAlign: "center" }}>
      <div style={{
        border: `1px solid ${on ? C.dcAccent : C.line}`, borderRadius: 7, padding: "8px 4px",
        background: on ? "rgba(242,177,56,0.08)" : "transparent",
      }}>
        <div style={{ fontFamily: "Chakra Petch, sans-serif", fontSize: 12, fontWeight: 700, color: on ? C.dcAccent : C.muted }}>{label}</div>
        <div style={{ fontFamily: "IBM Plex Mono, monospace", fontSize: 10.5, color: C.muted, marginTop: 2 }}>{sub}</div>
      </div>
    </div>
  );
  const arrow = (on) => (
    <div style={{ color: on ? C.dcAccent : C.faint, fontSize: 14, padding: "0 3px", alignSelf: "center" }}>›</div>
  );

  return (
    <div style={styles.panel}>
      <PanelHead>
        800VDC AI RACK
        <span style={{ marginLeft: "auto", fontSize: 11, color: buffering ? C.volt : live ? C.ok : C.muted, fontFamily: "IBM Plex Mono, monospace" }}>
          {buffering ? "● STORAGE BUFFERING" : live ? "● LIVE" : "○ representative"}
        </span>
      </PanelHead>

      {chip && (
        <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 10 }}>
          <ChipIcon color={chip.color} colorB={chip.colorB} size={20} />
          <span style={{ fontFamily: "IBM Plex Mono, monospace", fontSize: 11, color: C.muted }}>
            {chip.name} · {chip.gpus}× ~{chip.wGpu} W · rack ~{chip.rackKW} kW
            {chip.mix
              ? <span style={{ color: C.faint }}> · heterogeneous power profile</span>
              : !chip.dense && <span style={{ color: C.faint }}> · 54V-class</span>}
          </span>
        </div>
      )}

      {/* power chain */}
      <div style={{ display: "flex", alignItems: "stretch", marginBottom: 12 }}>
        {stage("GRID AC", "3-phase", running)}
        {arrow(running)}
        {stage("RECTIFIER", "AC/DC", running)}
        {arrow(running)}
        {stage("800VDC BUS", `${Math.round(grid)} kW`, running)}
        {arrow(running)}
        {stage("DC-DC", "per tray", running)}
        {arrow(running)}
        {stage("GPU TRAYS", chip ? `${chip.gpus}×` : `${N}×`, running)}
      </div>

      {/* GPU trays */}
      <div style={{ display: "flex", gap: 4, alignItems: "flex-end", height: 46, marginBottom: 4 }}>
        {trays.map((kw, i) => {
          const h = Math.max(3, (kw / maxTray) * 46);
          return (
            <div key={i} style={{ flex: 1, display: "flex", flexDirection: "column", justifyContent: "flex-end", height: "100%" }}>
              <div style={{
                height: h, background: trayFill(kw, i), borderRadius: 2,
                transition: reduced ? "none" : "height .2s ease, background .2s ease",
              }} />
            </div>
          );
        })}
      </div>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 12 }}>
        <span style={styles.tileSub}>{N} power trays · {Math.round(pTray)} kW each</span>
        <span style={styles.tileSub}>rack {Math.round(rack)} kW</span>
      </div>

      {/* efficiency vs baseline */}
      <div style={{ marginBottom: 10 }}>
        <EffBar label="800VDC end-to-end" value={e2e} color={C.ok} />
        <EffBar label="54V baseline" value={base} color={C.faint} />
        <div style={{ textAlign: "right", fontFamily: "IBM Plex Mono, monospace", fontSize: 11, color: C.ok, marginTop: 2 }}>
          +{gain}% efficiency · ~30% lower TCO
        </div>
      </div>

      {/* energy storage */}
      <div style={{ display: "flex", alignItems: "center", gap: 10 }}>
        <span style={styles.tileLabel}>ENERGY STORAGE</span>
        <div style={{ flex: 1, ...styles.barTrack, height: 10 }}>
          <div style={{ height: "100%", width: `${clamp(soc, 0, 100)}%`, background: buffering ? C.volt : C.dcAccent, borderRadius: 6, transition: reduced ? "none" : "width .25s ease" }} />
        </div>
        <span style={{ fontFamily: "IBM Plex Mono, monospace", fontSize: 11, color: buffering ? C.volt : C.muted, minWidth: 96, textAlign: "right" }}>
          {Math.round(soc)}% · {sp > 0 ? `−${Math.round(sp)}kW` : sp < 0 ? `+${Math.round(-sp)}kW` : "idle"}
        </span>
      </div>
      <div style={{ ...styles.tileSub, marginTop: 4 }}>
        Storage absorbs GPU load transients so grid draw stays flat.
      </div>
    </div>
  );
}

function EffBar({ label, value, color }) {
  return (
    <div style={{ display: "flex", alignItems: "center", gap: 8, marginBottom: 4 }}>
      <span style={{ ...styles.tileSub, width: 130, flexShrink: 0 }}>{label}</span>
      <div style={{ flex: 1, height: 8, background: C.lineSoft, borderRadius: 4, overflow: "hidden" }}>
        <div style={{ height: "100%", width: `${clamp(value, 0, 100)}%`, background: color, borderRadius: 4, transition: "width .3s ease" }} />
      </div>
      <span style={{ fontFamily: "IBM Plex Mono, monospace", fontSize: 11, color, width: 44, textAlign: "right" }}>
        {value ? value.toFixed(1) : "—"}%
      </span>
    </div>
  );
}

function StateStrip({ phase, accent }) {
  const idx = SEQUENCE.indexOf(phase);
  const faulted = phase === PHASE.FAULT;
  const complete = phase === PHASE.COMPLETE;
  return (
    <div style={styles.strip}>
      {SEQUENCE.map((p, i) => {
        const done = (idx > i && !faulted) || complete;
        const active = idx === i && !faulted;
        const col = active ? accent : done ? C.ok : C.faint;
        return (
          <React.Fragment key={p}>
            <div style={{ display: "flex", alignItems: "center", gap: 8, opacity: active || done ? 1 : 0.55 }}>
              <span
                style={{
                  width: 9,
                  height: 9,
                  borderRadius: "50%",
                  background: col,
                  boxShadow: active ? `0 0 10px ${col}` : "none",
                }}
              />
              <span style={{ ...styles.stripLabel, color: active ? C.text : C.muted }}>
                {PHASE_LABEL[p]}
              </span>
            </div>
            {i < SEQUENCE.length - 1 && (
              <span style={{ flex: 1, height: 2, background: (idx > i && !faulted) || complete ? C.ok : C.line, minWidth: 16 }} />
            )}
          </React.Fragment>
        );
      })}
      <div style={{ marginLeft: "auto", display: "flex", alignItems: "center", gap: 8 }}>
        <span style={{ width: 9, height: 9, borderRadius: "50%", background: faulted ? C.fault : C.faint, boxShadow: faulted ? `0 0 10px ${C.fault}` : "none" }} />
        <span style={{ ...styles.stripLabel, color: faulted ? C.fault : C.faint }}>Protection trip</span>
      </div>
    </div>
  );
}

function CanLog({ rows }) {
  return (
    <div style={styles.canScroll}>
      <div style={styles.canHeadRow}>
        <span style={{ width: 42 }}>t/s</span>
        <span style={{ width: 26 }}>dir</span>
        <span style={{ width: 62 }}>proto</span>
        <span style={{ width: 44 }}>addr</span>
        <span style={{ flex: 1 }}>message</span>
        <span style={{ width: 88, textAlign: "right" }}>data</span>
      </div>
      {rows.length === 0 && (
        <div style={{ color: C.faint, fontFamily: "IBM Plex Mono, monospace", fontSize: 12, padding: "14px 4px" }}>
          Bus idle — press START to open a session.
        </div>
      )}
      {rows.map((r) => {
        const isEstop = r.name === "Emergency_Stop" || r.name === "STATUS_WORD" || r.name === "Alarm_Flags set";
        const protoCol = r.proto === "PMBus" ? C.volt : r.proto === "MODBUS" ? C.evAccent : C.amp;
        return (
          <div key={r.k} style={{ ...styles.canRow, color: isEstop ? C.fault : C.text }}>
            <span style={{ width: 42, color: C.faint }}>{r.t.toFixed(1)}</span>
            <span style={{ width: 26, color: r.dir === "tx" ? C.amp : C.volt }}>{r.dir === "tx" ? "▸" : "◂"}</span>
            <span style={{ width: 62, color: protoCol }}>{r.proto ? r.proto : r.id}</span>
            <span style={{ width: 44, color: C.faint }}>{r.proto ? r.id : ""}</span>
            <span style={{ flex: 1, color: isEstop ? C.fault : C.text }}>{r.name}</span>
            <span style={{ width: 88, textAlign: "right", color: C.faint }}>{r.data}</span>
          </div>
        );
      })}
    </div>
  );
}

function Ctrl({ label, onClick, tone, disabled }) {
  const bg = tone === "go" ? C.ok : "transparent";
  const bd = tone === "go" ? C.ok : C.line;
  const fg = tone === "go" ? "#06121A" : disabled ? C.faint : C.text;
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        ...styles.ctrl,
        background: disabled ? "transparent" : bg,
        borderColor: disabled ? C.lineSoft : bd,
        color: disabled ? C.faint : fg,
        cursor: disabled ? "not-allowed" : "pointer",
      }}
    >
      {label}
    </button>
  );
}

function FaultBtn({ label, onClick, armed, disabled }) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      style={{
        ...styles.faultBtn,
        borderColor: armed ? C.fault : disabled ? C.lineSoft : C.line,
        color: disabled ? C.faint : armed ? C.fault : C.muted,
        background: armed ? "rgba(241,76,76,0.12)" : "transparent",
        cursor: disabled ? "not-allowed" : "pointer",
      }}
    >
      {label}
    </button>
  );
}

const PanelHead = ({ children }) => (
  <div style={styles.panelHead}>{children}</div>
);

const Legend = ({ color, text }) => (
  <span style={{ display: "inline-flex", alignItems: "center", gap: 6, color: C.muted, fontSize: 11 }}>
    <span style={{ width: 14, height: 3, background: color, borderRadius: 2 }} />
    {text}
  </span>
);

/* ============================= helpers ============================= */

function polar(cx, cy, r, angFromTopDeg) {
  const a = (angFromTopDeg * Math.PI) / 180;
  return [cx + r * Math.sin(a), cy - r * Math.cos(a)];
}
function clamp(v, lo, hi) {
  return Math.max(lo, Math.min(hi, v));
}
function fmt(v, d = 0) {
  if (v === null || v === undefined || Number.isNaN(v)) return "—";
  return Number(v).toLocaleString("en-US", { minimumFractionDigits: d, maximumFractionDigits: d });
}

/* ============================= styles ============================= */

const styles = {
  root: (faulted, flash) => ({
    fontFamily: "Inter, system-ui, sans-serif",
    background: C.bg,
    color: C.text,
    height: "100vh",
    overflow: "hidden",
    padding: "18px 20px 18px",
    boxSizing: "border-box",
    display: "flex",
    flexDirection: "column",
    gap: 12,
    boxShadow: flash ? `inset 0 0 0 3px ${C.fault}, inset 0 0 90px rgba(241,76,76,0.25)` : "none",
    transition: "box-shadow .2s ease",
    backgroundImage:
      "radial-gradient(circle at 18% 0%, rgba(45,212,167,0.05), transparent 42%), radial-gradient(circle at 90% 8%, rgba(242,177,56,0.05), transparent 40%)",
  }),
  header: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    gap: 16,
    flexWrap: "wrap",
    paddingBottom: 12,
    borderBottom: `1px solid ${C.line}`,
  },
  brand: {
    fontFamily: "Chakra Petch, sans-serif",
    fontSize: 34,
    fontWeight: 700,
    letterSpacing: 1.5,
  },
  brandSub: {
    fontFamily: "IBM Plex Mono, monospace",
    fontSize: 14,
    color: C.muted,
    letterSpacing: 2,
  },
  stdTag: {
    fontFamily: "IBM Plex Mono, monospace",
    fontSize: 14,
    color: C.muted,
    border: `1px solid ${C.line}`,
    borderRadius: 5,
    padding: "7px 12px",
    letterSpacing: 1,
  },
  busCaption: {
    fontFamily: "IBM Plex Mono, monospace",
    fontSize: 11,
    lineHeight: 1.45,
    color: C.muted,
    borderLeft: `2px solid ${C.line}`,
    paddingLeft: 8,
    marginBottom: 8,
  },
  toggleWrap: {
    display: "flex",
    border: `1px solid ${C.line}`,
    borderRadius: 7,
    padding: 3,
    gap: 3,
    background: C.panel,
  },
  toggleBtn: {
    fontFamily: "Chakra Petch, sans-serif",
    fontSize: 15,
    fontWeight: 700,
    letterSpacing: 1,
    border: "none",
    borderRadius: 5,
    padding: "9px 16px",
    transition: "all .18s ease",
  },
  statePill: {
    display: "inline-flex",
    alignItems: "center",
    gap: 8,
    border: "1px solid",
    borderRadius: 20,
    padding: "8px 16px",
    fontFamily: "Chakra Petch, sans-serif",
    fontSize: 15,
    fontWeight: 600,
    letterSpacing: 0.5,
  },
  dot: { width: 8, height: 8, borderRadius: "50%" },
  grid: {
    display: "grid",
    gridTemplateColumns: "minmax(280px, 1fr) minmax(320px, 1.35fr) minmax(300px, 1.1fr)",
    gap: 14,
    alignItems: "stretch",
  },
  col: { display: "flex", flexDirection: "column", gap: 12, minHeight: 0 },
  panel: {
    background: C.panel,
    border: `1px solid ${C.line}`,
    borderRadius: 10,
    padding: "12px 14px",
  },
  panelHead: {
    display: "flex",
    alignItems: "center",
    fontFamily: "IBM Plex Mono, monospace",
    fontSize: 11.5,
    letterSpacing: 2,
    color: C.muted,
    textTransform: "uppercase",
    marginBottom: 8,
  },
  tileRow: { display: "flex", gap: 12 },
  tileLabel: {
    fontFamily: "IBM Plex Mono, monospace",
    fontSize: 11,
    letterSpacing: 1.5,
    color: C.muted,
    textTransform: "uppercase",
  },
  tileVal: { fontFamily: "Chakra Petch, sans-serif", fontSize: 26, fontWeight: 700, lineHeight: 1.1 },
  tileUnit: { fontFamily: "IBM Plex Mono, monospace", fontSize: 12, color: C.muted },
  tileSub: { fontFamily: "IBM Plex Mono, monospace", fontSize: 11, color: C.faint, marginTop: 2 },
  gaugeVal: { fontFamily: "Chakra Petch, sans-serif", fontSize: 32, fontWeight: 700, lineHeight: 1 },
  gaugeUnit: { fontFamily: "IBM Plex Mono, monospace", fontSize: 12, color: C.muted, marginTop: 2 },
  barTrack: { height: 12, background: C.lineSoft, borderRadius: 6, overflow: "hidden" },
  barFill: { height: "100%", borderRadius: 6, transition: "width .25s ease" },
  legendRow: { display: "flex", alignItems: "center", gap: 16, marginTop: 6 },
  canScroll: {
    overflowY: "auto",
    maxHeight: 300,
    minHeight: 150,
    paddingRight: 2,
    overscrollBehavior: "contain",
    overflowAnchor: "none",
  },
  canHeadRow: {
    display: "flex",
    gap: 6,
    fontFamily: "IBM Plex Mono, monospace",
    fontSize: 10.5,
    letterSpacing: 1,
    color: C.faint,
    textTransform: "uppercase",
    padding: "0 2px 6px",
    borderBottom: `1px solid ${C.lineSoft}`,
    position: "sticky",
    top: 0,
    background: C.panel,
  },
  canRow: {
    display: "flex",
    gap: 6,
    fontFamily: "IBM Plex Mono, monospace",
    fontSize: 12.5,
    padding: "3px 2px",
    borderBottom: `1px solid ${C.lineSoft}`,
  },
  strip: {
    display: "flex",
    alignItems: "center",
    gap: 10,
    background: C.panel,
    border: `1px solid ${C.line}`,
    borderRadius: 10,
    padding: "12px 16px",
    flexWrap: "wrap",
  },
  stripLabel: { fontFamily: "Chakra Petch, sans-serif", fontSize: 12.5, fontWeight: 600, letterSpacing: 0.4 },
  faultBanner: {
    display: "flex",
    alignItems: "center",
    gap: 16,
    flexWrap: "wrap",
    background: "rgba(241,76,76,0.10)",
    border: `1px solid ${C.fault}`,
    borderRadius: 10,
    padding: "12px 16px",
  },
  faultCode: {
    fontFamily: "IBM Plex Mono, monospace",
    fontWeight: 700,
    fontSize: 14,
    color: C.fault,
    border: `1px solid ${C.fault}`,
    borderRadius: 5,
    padding: "3px 8px",
  },
  faultName: { fontFamily: "Chakra Petch, sans-serif", fontWeight: 700, fontSize: 16, color: C.text },
  faultMeta: { fontFamily: "IBM Plex Mono, monospace", fontSize: 11.5, color: C.muted, marginLeft: "auto" },
  completeBanner: {
    display: "flex",
    alignItems: "center",
    gap: 16,
    flexWrap: "wrap",
    background: "rgba(66,226,126,0.10)",
    border: `1px solid ${C.ok}`,
    borderRadius: 10,
    padding: "12px 16px",
  },
  completeCheck: {
    fontFamily: "IBM Plex Mono, monospace",
    fontWeight: 700,
    fontSize: 16,
    color: C.ok,
    border: `1px solid ${C.ok}`,
    borderRadius: 5,
    padding: "1px 9px",
  },
  completeName: { fontFamily: "Chakra Petch, sans-serif", fontWeight: 700, fontSize: 16, color: C.ok },
  main: {
    flex: 1,
    minHeight: 0,
    overflowY: "auto",
    overflowX: "hidden",
    overscrollBehavior: "contain",
    overflowAnchor: "none",
    display: "flex",
    flexDirection: "column",
    paddingRight: 2,
  },
  footer: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    gap: 16,
    flexWrap: "wrap",
    borderTop: `1px solid ${C.line}`,
    paddingTop: 12,
  },
  ctrlGroup: { display: "flex", gap: 10 },
  ctrl: {
    fontFamily: "Chakra Petch, sans-serif",
    fontSize: 13,
    fontWeight: 700,
    letterSpacing: 1.5,
    border: "1px solid",
    borderRadius: 7,
    padding: "10px 22px",
    transition: "all .15s ease",
  },
  faultGroup: { display: "flex", alignItems: "center", gap: 8, flexWrap: "wrap" },
  faultGroupLabel: {
    fontFamily: "IBM Plex Mono, monospace",
    fontSize: 11,
    letterSpacing: 1.5,
    color: C.faint,
    marginRight: 2,
  },
  faultBtn: {
    fontFamily: "IBM Plex Mono, monospace",
    fontSize: 11.5,
    letterSpacing: 0.5,
    border: "1px solid",
    borderRadius: 6,
    padding: "8px 12px",
    transition: "all .15s ease",
  },
};

function FontsAndKeyframes() {
  return (
    <style>{`
      @import url('https://fonts.googleapis.com/css2?family=Chakra+Petch:wght@500;600;700&family=IBM+Plex+Mono:wght@400;500;600&family=Inter:wght@400;500;600&display=swap');
      * { box-sizing: border-box; }
      button:focus-visible { outline: 2px solid ${C.evAccent}; outline-offset: 2px; }
      ::-webkit-scrollbar { width: 8px; }
      ::-webkit-scrollbar-track { background: transparent; }
      ::-webkit-scrollbar-thumb { background: ${C.line}; border-radius: 4px; }
      @media (max-width: 900px) {
        .vb-grid { grid-template-columns: 1fr !important; }
      }
    `}</style>
  );
}

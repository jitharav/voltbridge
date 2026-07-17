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
  bg: "#0B1015",
  panel: "#121A22",
  panel2: "#16212B",
  line: "#223140",
  lineSoft: "#1A2733",
  text: "#E6EDF3",
  muted: "#7D8FA0",
  faint: "#4A5A68",
  volt: "#F2B138", // bus voltage — gold
  amp: "#33C6DD", // current — cyan
  power: "#A78BFA", // power — violet
  ok: "#3FB950", // nominal — green
  warn: "#E3873C", // warning — orange
  fault: "#F14C4C", // trip — red
  evAccent: "#2DD4A7", // EV mode accent
  dcAccent: "#F2B138", // data-center mode accent
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

export default function VoltBridge() {
  const sim = useRef(fresh("ev"));
  const [, setTick] = useState(0);
  const flash = useRef(0); // fault flash timer
  const rmMotion = useRef(false);

  useEffect(() => {
    rmMotion.current =
      typeof window !== "undefined" &&
      window.matchMedia &&
      window.matchMedia("(prefers-reduced-motion: reduce)").matches;
  }, []);

  const pushCan = (s, key, bytes, dir) => {
    const map = CAN_NAMES[s.mode][key];
    if (!map) return;
    s.can.unshift({
      k: s.canId++,
      t: s.t,
      id: map[0],
      name: map[1],
      data: bytes.map(hx).join(" "),
      dir, // "tx" from EVSE/PSU, "rx" to it
    });
    if (s.can.length > 60) s.can.length = 60;
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
    const maxP = isDC ? 1_000_000 : 240_000; // W
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
        isDC ? [0x03, 0x20, 0x04, 0xe2] : [0x03, 0x20, 0x01, 0x2c],
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
    if (s.phase === PHASE.PRECHARGE)
      vTarget = 800 * Math.min(1, s.phaseT / PHASE_DUR.PRECHARGE);
    else if (s.phase === PHASE.TRANSFER) vTarget = 800;
    else if (s.phase === PHASE.SHUTDOWN || s.phase === PHASE.FAULT) vTarget = 0;

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
        // load ramps to setpoint then hovers
        const set = 86;
        s.load += clamp(set - s.load, -40 * DT, 45 * DT);
        s.load += (Math.random() - 0.5) * 0.6;
        const p = (s.load / 100) * maxP;
        iTarget = p / Math.max(1, s.vBus);
      } else {
        // CC-CV lithium curve
        const iMax = 300;
        let i = iMax;
        if (s.soc >= 80) i = iMax * Math.max(0.08, 1 - (s.soc - 80) / 20 * 0.9);
        iTarget = i * Math.min(1, s.phaseT / 1.0);
        s.soc = Math.min(100, s.soc + (iTarget * DT) / 90);
        if (s.soc >= 99.5) {
          pushCan(s, "stop", [0x02], "tx");
          s.phase = PHASE.SHUTDOWN;
          s.phaseT = 0;
          s.contactor = false;
        }
      }
    }
    // injected overcurrent spike
    if (s.injected === "oc" && s.phase === PHASE.TRANSFER)
      iTarget = isDC ? 1500 : 470;

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
    const pNow = (s.vBus * s.iBus) / 1000; // kW
    const heat = (Math.abs(s.vBus * s.iBus) / maxP) * 60;
    let cool = (s.temp - 25) * 0.28;
    if (s.injected === "ot") cool -= 130; // runaway
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
      else if (s.iBus > (isDC ? 1320 : 340))
        trip(s, "F-OC-03", "Overcurrent · contactor protection");
      else if (s.temp > LIMIT.tempMaxC)
        trip(s, "F-OT-04", "Power module over-temperature");
    }

    // ----- periodic CAN traffic -----
    if (s.phase === PHASE.HANDSHAKE && Math.abs(s.phaseT - 0.3) < DT / 2) {
      pushCan(s, "hsA", [0x11, 0x08, 0x00, 0x00], "tx");
      pushCan(s, "hsB", [0x11, 0x08, 0x03, 0x20], "rx");
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
  const maxP = isDC ? 1000 : 240; // kW
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
          <div style={styles.stdTag}>IS 17017 · ACAN</div>
          <ModeToggle mode={s.mode} onChange={setMode} disabled={s.phase !== PHASE.IDLE} />
          <StatePill phase={s.phase} accent={accent} />
        </div>
      </header>

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
            max={isDC ? 1400 : 360}
            unit="A"
            color={C.amp}
            warnAt={isDC ? 1320 : 340}
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
          <SocBar isDC={isDC} soc={s.soc} load={s.load} accent={accent} />
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
                  <YAxis yAxisId="i" orientation="right" domain={[0, isDC ? 1400 : 360]} width={40} tick={{ fill: C.amp, fontSize: 10, fontFamily: "IBM Plex Mono, monospace" }} axisLine={{ stroke: C.line }} tickLine={false} />
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
        </section>

        {/* right: CAN log */}
        <section style={styles.col}>
          <div style={{ ...styles.panel, flex: 1, display: "flex", flexDirection: "column", minHeight: 0 }}>
            <PanelHead>
              ACAN BUS · IS 17017
              <span style={{ marginLeft: "auto", fontSize: 10, color: s.injected === "comms" ? C.fault : C.ok, fontFamily: "IBM Plex Mono, monospace" }}>
                {s.injected === "comms" ? "● LINK LOST" : "● LINK UP"}
              </span>
            </PanelHead>
            <CanLog rows={s.can} />
          </div>
        </section>
      </div>

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

      {/* ---------- controls ---------- */}
      <footer style={styles.footer}>
        <div style={styles.ctrlGroup}>
          <Ctrl label="START" onClick={start} tone="go" disabled={s.phase !== PHASE.IDLE} />
          <Ctrl label="STOP" onClick={stop} tone="neutral" disabled={s.phase === PHASE.IDLE || faulted} />
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

const disabledInject = (s) => s.phase === PHASE.IDLE || s.phase === PHASE.FAULT;

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

function StatePill({ phase, accent }) {
  const faulted = phase === PHASE.FAULT;
  const running = phase !== PHASE.IDLE && !faulted;
  const color = faulted ? C.fault : running ? accent : C.faint;
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

function SocBar({ isDC, soc, load, accent }) {
  const pct = isDC ? load : soc;
  return (
    <div style={{ ...styles.panel, padding: "12px 14px" }}>
      <div style={{ display: "flex", justifyContent: "space-between", marginBottom: 8 }}>
        <span style={styles.tileLabel}>{isDC ? "IT LOAD" : "STATE OF CHARGE"}</span>
        <span style={{ fontFamily: "IBM Plex Mono, monospace", color: accent, fontSize: 13, fontWeight: 600 }}>
          {fmt(pct, 0)}%
        </span>
      </div>
      <div style={styles.barTrack}>
        <div style={{ ...styles.barFill, width: `${clamp(pct, 0, 100)}%`, background: accent }} />
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

function StateStrip({ phase, accent }) {
  const idx = SEQUENCE.indexOf(phase);
  const faulted = phase === PHASE.FAULT;
  return (
    <div style={styles.strip}>
      {SEQUENCE.map((p, i) => {
        const done = idx > i && !faulted;
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
              <span style={{ flex: 1, height: 2, background: idx > i && !faulted ? C.ok : C.line, minWidth: 16 }} />
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
        <span style={{ width: 52 }}>id</span>
        <span style={{ flex: 1 }}>message</span>
        <span style={{ width: 96, textAlign: "right" }}>data</span>
      </div>
      {rows.length === 0 && (
        <div style={{ color: C.faint, fontFamily: "IBM Plex Mono, monospace", fontSize: 12, padding: "14px 4px" }}>
          Bus idle — press START to open a session.
        </div>
      )}
      {rows.map((r) => {
        const isEstop = r.name === "Emergency_Stop";
        return (
          <div key={r.k} style={{ ...styles.canRow, color: isEstop ? C.fault : C.text }}>
            <span style={{ width: 42, color: C.faint }}>{r.t.toFixed(1)}</span>
            <span style={{ width: 26, color: r.dir === "tx" ? C.amp : C.volt }}>{r.dir === "tx" ? "▸" : "◂"}</span>
            <span style={{ width: 52, color: C.muted }}>{r.id}</span>
            <span style={{ flex: 1, color: isEstop ? C.fault : C.text }}>{r.name}</span>
            <span style={{ width: 96, textAlign: "right", color: C.faint }}>{r.data}</span>
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
    minHeight: "100vh",
    padding: "18px 20px 22px",
    boxSizing: "border-box",
    display: "flex",
    flexDirection: "column",
    gap: 14,
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
    fontSize: 26,
    fontWeight: 700,
    letterSpacing: 1.5,
  },
  brandSub: {
    fontFamily: "IBM Plex Mono, monospace",
    fontSize: 11,
    color: C.muted,
    letterSpacing: 2,
  },
  stdTag: {
    fontFamily: "IBM Plex Mono, monospace",
    fontSize: 11,
    color: C.muted,
    border: `1px solid ${C.line}`,
    borderRadius: 4,
    padding: "5px 9px",
    letterSpacing: 1,
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
    fontSize: 12,
    fontWeight: 700,
    letterSpacing: 1,
    border: "none",
    borderRadius: 5,
    padding: "7px 13px",
    transition: "all .18s ease",
  },
  statePill: {
    display: "inline-flex",
    alignItems: "center",
    gap: 8,
    border: "1px solid",
    borderRadius: 20,
    padding: "6px 14px",
    fontFamily: "Chakra Petch, sans-serif",
    fontSize: 13,
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
    fontSize: 10.5,
    letterSpacing: 2,
    color: C.muted,
    textTransform: "uppercase",
    marginBottom: 8,
  },
  tileRow: { display: "flex", gap: 12 },
  tileLabel: {
    fontFamily: "IBM Plex Mono, monospace",
    fontSize: 10,
    letterSpacing: 1.5,
    color: C.muted,
    textTransform: "uppercase",
  },
  tileVal: { fontFamily: "Chakra Petch, sans-serif", fontSize: 26, fontWeight: 700, lineHeight: 1.1 },
  tileUnit: { fontFamily: "IBM Plex Mono, monospace", fontSize: 12, color: C.muted },
  tileSub: { fontFamily: "IBM Plex Mono, monospace", fontSize: 10, color: C.faint, marginTop: 2 },
  gaugeVal: { fontFamily: "Chakra Petch, sans-serif", fontSize: 32, fontWeight: 700, lineHeight: 1 },
  gaugeUnit: { fontFamily: "IBM Plex Mono, monospace", fontSize: 12, color: C.muted, marginTop: 2 },
  barTrack: { height: 12, background: C.lineSoft, borderRadius: 6, overflow: "hidden" },
  barFill: { height: "100%", borderRadius: 6, transition: "width .25s ease" },
  legendRow: { display: "flex", alignItems: "center", gap: 16, marginTop: 6 },
  canScroll: {
    overflowY: "auto",
    flex: 1,
    minHeight: 180,
    paddingRight: 2,
  },
  canHeadRow: {
    display: "flex",
    gap: 6,
    fontFamily: "IBM Plex Mono, monospace",
    fontSize: 9.5,
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
    fontSize: 11.5,
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
  footer: {
    display: "flex",
    justifyContent: "space-between",
    alignItems: "center",
    gap: 16,
    flexWrap: "wrap",
    borderTop: `1px solid ${C.line}`,
    paddingTop: 14,
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
    fontSize: 10,
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

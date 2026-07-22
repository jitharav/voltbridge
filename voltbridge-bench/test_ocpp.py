#!/usr/bin/env python3
"""Offline self-test for ocpp_gateway.py — validates the OCPP 1.6-J message
builders and phase->status mapping. No broker/CSMS/network needed, so it runs
anywhere (used by CI)."""
import sys
import ocpp_gateway as g

SAMPLE = {"phase": "TRANSFER", "mode": "ev", "v_bus": 800.0, "i_bus": 338.0,
          "power_kw": 270.4, "soc": 63.1, "contactor": True, "fault": None}

checks = 0


def ok(cond, msg):
    global checks
    checks += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    print(f"ok: {msg}")


def main():
    # phase -> OCPP status mapping
    ok(g.ocpp_status("IDLE") == "Available", "IDLE -> Available")
    ok(g.ocpp_status("PRECHARGE") == "Preparing", "PRECHARGE -> Preparing")
    ok(g.ocpp_status("TRANSFER") == "Charging", "TRANSFER -> Charging")
    ok(g.ocpp_status("COMPLETE") == "Finishing", "COMPLETE -> Finishing")
    ok(g.ocpp_status("FAULT") == "Faulted", "FAULT -> Faulted")

    # BootNotification
    boot = g.boot_notification_payload()
    ok(boot["chargePointVendor"] == "VoltBridge", "boot vendor")
    ok("chargePointModel" in boot, "boot model")

    # StatusNotification
    sn = g.status_notification_payload("Charging")
    ok(sn["connectorId"] == 1 and sn["status"] == "Charging", "status notification ok")
    ok(sn["errorCode"] == "NoError", "status default NoError")

    # MeterValues
    mv = g.meter_values_payload(SAMPLE)
    sv = mv["meterValue"][0]["sampledValue"]
    by = {x["measurand"]: x for x in sv}
    ok("Voltage" in by and by["Voltage"]["value"] == "800.0" and by["Voltage"]["unit"] == "V", "MeterValues Voltage 800V")
    ok("Current.Import" in by and by["Current.Import"]["value"] == "338.0", "MeterValues Current 338A")
    ok("Power.Active.Import" in by and by["Power.Active.Import"]["unit"] == "kW", "MeterValues Power kW")
    ok("SoC" in by and by["SoC"]["value"] == "63.1" and by["SoC"]["unit"] == "Percent", "MeterValues SoC 63.1%")

    # SoC omitted when absent (e.g. DC telemetry)
    mv_dc = g.meter_values_payload({"v_bus": 800.0, "i_bus": 1320.0, "power_kw": 1056.0})
    by_dc = {x["measurand"] for x in mv_dc["meterValue"][0]["sampledValue"]}
    ok("SoC" not in by_dc, "SoC omitted when telemetry has none")

    # CALL frame shape
    cf = g.call_frame("7", "BootNotification", boot)
    ok(cf[0] == 2 and cf[1] == "7" and cf[2] == "BootNotification" and cf[3] == boot, "CALL frame [2,uid,action,payload]")

    print(f"\nAll {checks} OCPP gateway checks passed.")


if __name__ == "__main__":
    main()

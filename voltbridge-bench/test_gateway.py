#!/usr/bin/env python3
"""Offline self-test for redfish_gateway.py — validates the Redfish resource
builders produce well-formed output from a telemetry snapshot. No broker or
network needed, so it runs anywhere (used by CI)."""
import sys
import redfish_gateway as g

SAMPLE = {
    "phase": "TRANSFER", "mode": "dc", "v_bus": 800.0, "i_bus": 1320.0,
    "power_kw": 1056.0, "rack_power_kw": 1351.7, "grid_power_kw": 1056.0,
    "temp": 63.1, "rect_temp": 63.0, "contactor": True, "fault": None,
    "e2e_eff": 95.3, "baseline_eff": 90.5, "eff_gain": 4.8, "loss_kw": 63.2,
    "storage_soc": 62.1, "storage_power": 295.7, "buffering": True,
    "n_trays": 8, "trays": [169.0] * 8,
}

checks = 0


def ok(cond, msg):
    global checks
    checks += 1
    if not cond:
        print(f"FAIL: {msg}")
        sys.exit(1)
    print(f"ok: {msg}")


def main():
    g._latest.clear()
    g._latest.update(SAMPLE)
    g._msg_count = 1
    t = dict(SAMPLE)

    root = g.res_service_root()
    ok("ServiceRoot" in root["@odata.type"], "service root has ServiceRoot type")
    ok(root["Chassis"]["@odata.id"] == "/redfish/v1/Chassis", "service root links Chassis")

    coll = g.res_chassis_collection()
    ok(coll["Members@odata.count"] == 1, "chassis collection has 1 member")

    ch = g.res_chassis(t)
    ok(ch["Id"] == "Rack1", "chassis id Rack1")
    ok(ch["PowerState"] == "On", "chassis power state On during transfer")
    ok(ch["Status"]["Health"] == "OK", "chassis health OK when no fault")

    p = g.res_power(t)
    ok("Power" in p["@odata.type"], "power has Power type")
    ok(p["PowerControl"][0]["PowerConsumedWatts"] == round(1351.7 * 1000), "power consumed watts correct")
    ok(p["Voltages"][0]["ReadingVolts"] == 800.0, "voltage reading 800V")
    ok(p["Oem"]["VoltBridge"]["BusCurrentAmps"] == 1320.0, "current in Oem")

    th = g.res_thermal(t)
    ok(len(th["Temperatures"]) == 2, "thermal has rectifier + module")
    ok(all("ReadingCelsius" in x for x in th["Temperatures"]), "temps have ReadingCelsius")

    b = g.res_battery(t)
    ok("Battery" in b["@odata.type"], "battery has Battery type")
    ok(b["StateOfChargePercent"] == 62.1, "battery SoC 62.1")
    ok(b["Oem"]["VoltBridge"]["Buffering"] is True, "battery buffering flag")

    # fault path -> Critical health
    tf = dict(SAMPLE, fault="F-OT-04")
    ok(g.res_chassis(tf)["Status"]["Health"] == "Critical", "fault -> chassis Critical")

    # routing table resolves every advertised endpoint
    for path in ["/redfish/v1/", "/redfish/v1/Chassis", "/redfish/v1/Chassis/Rack1",
                 "/redfish/v1/Chassis/Rack1/Power", "/redfish/v1/Chassis/Rack1/Thermal",
                 "/redfish/v1/Chassis/Rack1/Battery"]:
        key = path.rstrip("/") or "/"
        builder = g.ROUTES.get(key) or g.ROUTES.get(key + "/") or g.ROUTES.get(path)
        ok(builder is not None, f"route resolves: {path}")

    print(f"\nAll {checks} Redfish gateway checks passed.")


if __name__ == "__main__":
    main()

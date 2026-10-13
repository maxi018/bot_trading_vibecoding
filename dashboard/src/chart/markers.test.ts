import { describe, expect, it } from "vitest";
import { prepareMarkers, snapToCandle, toMarker } from "./markers";

const H = 3_600_000;
const C = { entry: "#1", exit: "#2", stop: "#3" };

describe("snapToCandle", () => {
  it("un fill en el cierre de la vela K queda en K", () => {
    // vela K abre en 10h, cierra en 11h; fill exactamente a las 11h
    expect(snapToCandle(11 * H, H, null)).toBe((10 * H) / 1000);
  });
  it("un fill dentro de la vela queda en esa vela", () => {
    expect(snapToCandle(10 * H + 5, H, null)).toBe((10 * H) / 1000);
  });
  it("nunca después de la última vela dibujada", () => {
    expect(snapToCandle(50 * H, H, (12 * H) / 1000)).toBe((12 * H) / 1000);
  });
});

describe("toMarker", () => {
  it("compra = flecha arriba bajo la vela; stop en su color", () => {
    const m = toMarker({ ts: H, side: "buy", qty: 0.0834, price: 1, tag: "entry" }, H, null, C);
    expect(m).toMatchObject({ shape: "arrowUp", position: "belowBar", color: "#1" });
    expect(m.text).toBe("ENTRADA 0.0834");
    const s = toMarker({ ts: H, side: "sell", qty: 1, price: 1, tag: "stop" }, H, null, C);
    expect(s).toMatchObject({ shape: "arrowDown", position: "aboveBar", color: "#3" });
  });
});

describe("prepareMarkers", () => {
  it("ordena y deja texto solo en el último", () => {
    const a = toMarker({ ts: 3 * H, side: "buy", qty: 1, price: 1, tag: "entry" }, H, null, C);
    const b = toMarker({ ts: 1 * H, side: "sell", qty: 1, price: 1, tag: "exit" }, H, null, C);
    const out = prepareMarkers([a, b]);
    expect(out.map((m) => m.time)).toEqual([0, (2 * H) / 1000]);
    expect(out[0]!.text).toBe("");
    expect(out[1]!.text).toBe("ENTRADA 1.00");
  });
});

import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, describe, expect, it, vi } from "vitest";
import type { Breaker } from "../api/types";
import { BreakerCard, LimitsEditor } from "./Risk";

afterEach(() => { vi.restoreAllMocks(); document.body.innerHTML = ""; });

const breaker: Breaker = {
  key: "total_drawdown", label: "DD total", value: 0.13, threshold: 0.15, level: 0.87,
  status: "red", tripped: false, action: "KILL + flatten",
};

describe("BreakerCard", () => {
  it("cerca del umbral vs disparado, siempre con texto (no solo color)", () => {
    const { rerender } = render(<BreakerCard b={breaker} />);
    expect(screen.getByText(/Cerca del umbral/)).toBeTruthy();
    expect(screen.getByText("13.00%")).toBeTruthy();
    rerender(<BreakerCard b={{ ...breaker, tripped: true }} />);
    expect(screen.getByText(/DISPARADO/)).toBeTruthy();
  });
});

const risk = (paused: boolean) => ({
  status: paused ? "PAUSED" : "RUNNING", paused, killed: false, halt_reasons: [], size_multiplier: 1,
  veto_count: 0, breakers: [],
  limits: [
    { name: "max_risk_per_trade", value: 0.01, editable: true, conservative_direction: "lower" as const },
    { name: "market_protection_bps", value: 10, editable: false, conservative_direction: null },
  ],
} as const);

function input() { return screen.getByLabelText(/Nuevo valor de Riesgo máx/) as HTMLInputElement; }

describe("LimitsEditor", () => {
  it("endurecer con el bot corriendo: se puede aplicar", () => {
    render(<LimitsEditor risk={risk(false) as never} mode="paper" onApplied={() => {}} />);
    fireEvent.change(input(), { target: { value: "0.5" } });
    expect(screen.getByText("✓ endurece")).toBeTruthy();
    expect((screen.getByText("Revisar y aplicar") as HTMLButtonElement).disabled).toBe(false);
    expect(screen.getByText("🔒 solo con reinicio")).toBeTruthy(); // no editable
  });

  it("relajar con el bot corriendo: bloqueado, pide pausar", () => {
    render(<LimitsEditor risk={risk(false) as never} mode="paper" onApplied={() => {}} />);
    fireEvent.change(input(), { target: { value: "2" } });
    expect(screen.getByText("⚠ relaja")).toBeTruthy();
    expect(screen.getByText(/Para relajar hay que pausar/)).toBeTruthy();
    expect((screen.getByText("Revisar y aplicar") as HTMLButtonElement).disabled).toBe(true);
  });

  it("relajar con el bot pausado: exige tildar la confirmación y manda confirm_relax", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ applied: [] }), { status: 200 }),
    );
    const onApplied = vi.fn();
    render(<LimitsEditor risk={risk(true) as never} mode="live" onApplied={onApplied} />);
    fireEvent.change(input(), { target: { value: "1.5" } });
    fireEvent.click(screen.getByText("Revisar y aplicar"));
    expect(screen.getByText(/Modo LIVE/)).toBeTruthy();
    const applyBtn = screen.getByRole("button", { name: "Aplicar" }) as HTMLButtonElement;
    expect(applyBtn.disabled).toBe(true);
    fireEvent.click(screen.getByRole("checkbox"));
    expect(applyBtn.disabled).toBe(false);
    fireEvent.click(applyBtn);
    await waitFor(() => expect(onApplied).toHaveBeenCalled());
    const body = JSON.parse(String(fetchMock.mock.calls[0]![1]!.body));
    expect(body).toMatchObject({ changes: { max_risk_per_trade: 0.015 }, confirm_relax: true });
  });

  it("un valor inválido no se puede aplicar", () => {
    render(<LimitsEditor risk={risk(true) as never} mode="paper" onApplied={() => {}} />);
    fireEvent.change(input(), { target: { value: "mucho" } });
    expect(screen.getByText("✕ inválido")).toBeTruthy();
    expect((screen.getByText("Revisar y aplicar") as HTMLButtonElement).disabled).toBe(true);
  });
});

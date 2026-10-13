import { useState, type FormEvent } from "react";
import { ApiError, api, auth } from "../api/client";

export function Login({ onLogin }: { onLogin: () => void }) {
  const [token, setToken] = useState("");
  const [operator, setOperator] = useState(auth.operator);
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  async function submit(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    try {
      await api.check(token.trim());
      auth.login(token.trim(), operator.trim());
      onLogin();
    } catch (err) {
      setError(err instanceof ApiError ? (err.status === 401 ? "Token incorrecto" : err.message) : "Error");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="login">
      <form className="card" onSubmit={submit}>
        <div className="brand" style={{ fontSize: 20 }}>quant<span>bot</span> <span className="muted">terminal</span></div>
        <p className="secondary">Ingresá el token del dashboard (variable <span className="mono">QB_DASHBOARD_TOKEN</span>).</p>
        <div className="field">
          <label htmlFor="token">Token</label>
          <input id="token" className="input mono" type="password" autoComplete="current-password"
            value={token} onChange={(e) => setToken(e.target.value)} required minLength={16} autoFocus />
        </div>
        <div className="field">
          <label htmlFor="operator">Operador (queda registrado en cada comando)</label>
          <input id="operator" className="input" value={operator} maxLength={64}
            onChange={(e) => setOperator(e.target.value)} required placeholder="tu nombre" />
        </div>
        {error && <div className="error-text" role="alert">{error}</div>}
        <div className="dialog-actions">
          <button className="btn primary" disabled={busy || token.trim().length < 16 || !operator.trim()}>
            {busy ? "Verificando…" : "Entrar"}
          </button>
        </div>
      </form>
    </div>
  );
}

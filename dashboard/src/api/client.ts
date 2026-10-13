// Cliente REST. El token vive en sessionStorage (se borra al cerrar la pestaña)
// y NUNCA va en la URL. Cualquier 401 cierra la sesión.

const TOKEN_KEY = "qb.token";
const OPERATOR_KEY = "qb.operator";

function safeGet(store: Storage | undefined, key: string): string {
  try {
    return store?.getItem(key) ?? "";
  } catch {
    return "";
  }
}

function safeSet(store: Storage | undefined, key: string, value: string | null): void {
  try {
    if (value === null) store?.removeItem(key);
    else store?.setItem(key, value);
  } catch {
    /* storage bloqueado: la sesión dura lo que la pestaña */
  }
}

const session = typeof sessionStorage !== "undefined" ? sessionStorage : undefined;
const local = typeof localStorage !== "undefined" ? localStorage : undefined;

let memToken = safeGet(session, TOKEN_KEY);
const logoutListeners = new Set<() => void>();

export const auth = {
  get token(): string {
    return memToken;
  },
  get operator(): string {
    return safeGet(local, OPERATOR_KEY);
  },
  login(token: string, operator: string): void {
    memToken = token;
    safeSet(session, TOKEN_KEY, token);
    safeSet(local, OPERATOR_KEY, operator);
  },
  logout(): void {
    memToken = "";
    safeSet(session, TOKEN_KEY, null);
    logoutListeners.forEach((fn) => fn());
  },
  onLogout(fn: () => void): () => void {
    logoutListeners.add(fn);
    return () => logoutListeners.delete(fn);
  },
};

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}

async function request<T>(method: string, path: string, body?: unknown, token = memToken): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers: {
        Authorization: `Bearer ${token}`,
        ...(body !== undefined ? { "Content-Type": "application/json" } : {}),
      },
      body: body !== undefined ? JSON.stringify(body) : undefined,
    });
  } catch {
    throw new ApiError(0, "Sin conexión con la API del bot");
  }
  if (res.status === 401) {
    if (token === memToken) auth.logout();
    throw new ApiError(401, "Token inválido");
  }
  const data: unknown = await res.json().catch(() => ({}));
  if (!res.ok) {
    const detail = (data as { detail?: unknown }).detail;
    const msg = typeof detail === "string" ? detail : `Error ${res.status}`;
    throw new ApiError(res.status, msg);
  }
  return data as T;
}

export const api = {
  get: <T>(path: string) => request<T>("GET", path),
  post: <T>(path: string, body: unknown) => request<T>("POST", path, body),
  patch: <T>(path: string, body: unknown) => request<T>("PATCH", path, body),
  /** Valida un token sin guardarlo (pantalla de login). */
  check: (token: string) => request<{ mode: string }>("GET", "/api/session", undefined, token),
};

// ---- comandos de operador (todos pasan por el risk engine del bot) ----
const op = () => ({ operator: auth.operator || "dashboard" });

export const commands = {
  kill: (reason: string) =>
    api.post<{ status: string }>("/api/control/kill", { ...op(), confirm: true, reason, flatten: true }),
  pause: (reason = "") => api.post<{ status: string }>("/api/control/pause", { ...op(), reason }),
  resume: () => api.post<{ status: string }>("/api/control/resume", op()),
  rearm: (confirmText: string) =>
    api.post<{ status: string }>("/api/control/rearm", { ...op(), confirm_text: confirmText }),
};

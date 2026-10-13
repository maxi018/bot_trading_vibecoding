import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import App from "./App";
import "./styles.css";

try {
  document.documentElement.dataset.theme = localStorage.getItem("qb.theme") ?? "dark";
} catch {
  /* sin storage: queda el tema oscuro por defecto */
}

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>,
);

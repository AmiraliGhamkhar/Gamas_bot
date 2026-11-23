import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import "@fontsource-variable/vazirmatn";
import App from "./App";
import "./index.css";
import { initTelegram } from "./lib/telegram";

initTelegram();

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <App />
  </StrictMode>
);

import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { createApi } from "./api/client";
import { defaultAuthProvider } from "./auth";
import { Chat } from "./components/Chat";
import "./styles.css";

const api = createApi({ auth: defaultAuthProvider() });
const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <Chat api={api} />
    </StrictMode>,
  );
}

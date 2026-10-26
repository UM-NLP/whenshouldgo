import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { createApi } from "./api/client";
import { defaultAuthProvider } from "./auth";
import { createBoardApi } from "./board/boardApi";
import { Chat } from "./components/Chat";
import { defaultLocalStore } from "./storage";
import "./styles.css";

const auth = defaultAuthProvider();
const api = createApi({ auth });
const boardApi = createBoardApi({ auth });
const local = defaultLocalStore();
const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <Chat api={api} local={local} boardApi={boardApi} />
    </StrictMode>,
  );
}

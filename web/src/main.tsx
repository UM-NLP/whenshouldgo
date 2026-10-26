import { StrictMode } from "react";
import { createRoot } from "react-dom/client";

import { createAccountApi } from "./account/accountApi";
import { createApi } from "./api/client";
import { createBoardApi } from "./board/boardApi";
import { Chat } from "./components/Chat";
import { createAuthService, providerFromService } from "./firebase";
import { createPhase1Api } from "./history/historyApi";
import { defaultLocalStore } from "./storage";
import "./styles.css";

// One sign-in service. A production build without VITE_FIREBASE_* config gets the fixed
// "not configured" service (no token is sent); the dev provider exists only in a dev build.
const authService = createAuthService();
const auth = providerFromService(authService);
const api = createApi({ auth });
const boardApi = createBoardApi({ auth });
const accountApi = createAccountApi({ auth });
const phase1Api = createPhase1Api({ auth });
const local = defaultLocalStore();
const root = document.getElementById("root");
if (root) {
  createRoot(root).render(
    <StrictMode>
      <Chat
        api={api}
        local={local}
        boardApi={boardApi}
        account={{ api: accountApi, auth: authService }}
        phase1={{ api: phase1Api, account: accountApi }}
      />
    </StrictMode>,
  );
}

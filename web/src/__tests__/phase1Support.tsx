import { render, screen } from "@testing-library/react";
import userEvent from "@testing-library/user-event";

import { createAccountApi } from "../account/accountApi";
import { DevAuthProvider } from "../auth";
import { createBoardApi } from "../board/boardApi";
import { Chat } from "../components/Chat";
import { DevAuthService, type DevAuthBehavior } from "../firebaseDev";
import type { AuthService } from "../firebase";
import { createPhase1Api } from "../history/historyApi";
import type { LocalStore } from "../storage";
import { pendingAction, workspace } from "./boardSupport";
import { bootstrapBody, errorBody, fakeBackend, json, turnResponse, usageView, type Handler } from "./support";
import type { UsageView } from "../api/client";

export const CONSENT = (purpose: string, version = "x") => ({
  id: `c-${purpose}`,
  purpose,
  notice_version: version,
  processors: ["GOOGLE"],
  granted_at: "2026-10-26T12:00:00Z",
});

export interface AppOptions {
  routes?: Record<string, Handler | Response>;
  tier?: "guest" | "registered";
  historyEnabled?: boolean;
  canLink?: boolean;
  usage?: UsageView;
  local?: LocalStore | null;
  authService?: AuthService;
  authBehavior?: DevAuthBehavior;
  /** Omit to render without the account / Phase 1 wiring (the pre-existing behavior). */
  wireAccount?: boolean;
  wirePhase1?: boolean;
}

export function renderApp(options: AppOptions = {}) {
  const { tier = "guest", historyEnabled = false, canLink = true, usage = usageView() } = options;
  const bootstrap = () => {
    const body = bootstrapBody(usage);
    return json({ ...body, tier, capabilities: { ...body.capabilities, history_enabled: historyEnabled, can_link_account: canLink } });
  };
  const backend = fakeBackend({
    "POST /v1/session:bootstrap": bootstrap,
    "POST /v1/agent/turns": json(turnResponse()),
    "GET /v1/workspace": json(workspace()),
    ...options.routes,
  });
  const apiOptions = { auth: new DevAuthProvider(), fetchImpl: backend.fetchImpl as unknown as typeof fetch, baseUrl: "http://localhost:5173" };
  const accountApi = createAccountApi(apiOptions);
  const phase1Api = createPhase1Api(apiOptions);
  const boardApi = createBoardApi(apiOptions);
  const authService = options.authService ?? new DevAuthService(options.authBehavior);
  const user = userEvent.setup();
  const view = render(
    <Chat
      api={backend.api}
      boardApi={boardApi}
      local={options.local ?? null}
      account={options.wireAccount === false ? null : { api: accountApi, auth: authService }}
      phase1={options.wirePhase1 === false ? null : { api: phase1Api, account: accountApi }}
    />,
  );
  const calls = (method: string, path: string) => backend.requests.filter((r) => r.method === method && r.url.split("?")[0] === path);
  return { ...backend, user, view, authService, calls, accountApi, phase1Api, boardApi };
}

export async function acceptLocalNotice(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "OK, save on this device" }));
}

export async function turnOnLocation(user: ReturnType<typeof userEvent.setup>) {
  await user.click(await screen.findByRole("button", { name: "Use my location" }));
  await user.click(screen.getByRole("button", { name: "Agree and use my location" }));
  await screen.findByRole("button", { name: "Stop using my location" });
}

export async function sendText(user: ReturnType<typeof userEvent.setup>, text: string) {
  await user.type(screen.getByLabelText("Your message"), text);
  await user.click(screen.getByRole("button", { name: "Send" }));
}

export { errorBody, json, pendingAction, turnResponse, usageView, workspace };

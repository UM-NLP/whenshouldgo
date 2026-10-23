import { render, screen, waitFor } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import { afterEach, describe, expect, it } from "vitest";

import { Chat } from "../components/Chat";
import { bootstrapBody, errorBody, fakeBackend, json, turnResponse, usageView } from "./support";

afterEach(() => {
  localStorage.clear();
  sessionStorage.clear();
});

function backendWith(usage = usageView(), turn = turnResponse()) {
  return fakeBackend({
    "POST /v1/session:bootstrap": json(bootstrapBody(usage)),
    "POST /v1/agent/turns": json(turn),
  });
}

describe("usage meter", () => {
  it("calls bootstrap on load with the bearer token and shows nothing at NONE", async () => {
    const backend = backendWith();
    render(<Chat api={backend.api} />);
    await waitFor(() => expect(backend.requests.some((r) => r.url === "/v1/session:bootstrap")).toBe(true));
    const call = backend.requests.find((r) => r.url === "/v1/session:bootstrap");
    expect(call?.method).toBe("POST");
    expect(call?.headers.get("authorization")).toMatch(/^Bearer /);
    expect(screen.queryByLabelText("Usage")).toBeNull();
    expect(screen.queryByText(/free account/)).toBeNull();
  });

  it("shows a quiet counter at QUIET_70 and no notice", async () => {
    const backend = backendWith(usageView({ meter_state: "QUIET_70", user_turns_used: 35 }));
    render(<Chat api={backend.api} />);
    expect(await screen.findByLabelText("Usage")).toHaveTextContent("Turns 35 of 50");
    expect(screen.queryByRole("button", { name: "Dismiss" })).toBeNull();
    expect(screen.queryByText(/free account/)).toBeNull();
  });

  it("shows one dismissible benefit-led notice at NOTICE_90", async () => {
    const backend = backendWith(usageView({ meter_state: "NOTICE_90", user_turns_used: 45 }));
    render(<Chat api={backend.api} />);
    expect(await screen.findByText(/recover your trips and keep them in sync/)).toBeInTheDocument();
    expect(screen.getByLabelText("Usage")).toHaveTextContent("Turns 45 of 50");
    expect(screen.getByLabelText("Your message")).toBeEnabled();
    await userEvent.setup().click(screen.getByRole("button", { name: "Dismiss" }));
    expect(screen.queryByText(/recover your trips/)).toBeNull();
    expect(screen.getByLabelText("Usage")).toBeInTheDocument();
  });

  it("at GATED (trips) shows the exact gate wording and disables sending", async () => {
    const backend = backendWith(
      usageView({ meter_state: "GATED", gate_reason: "TRIPS_LIMIT", activated_trips_used: 10 }),
    );
    render(<Chat api={backend.api} />);
    expect(
      await screen.findByText(
        "You used your 10 guest trips. Create a free account to continue and receive 10 trips every 30 days.",
      ),
    ).toBeInTheDocument();
    expect(screen.getByLabelText("Your message")).toBeDisabled();
    expect(screen.getByRole("button", { name: "Send" })).toBeDisabled();
    expect(screen.getByText(/still read, export and delete/)).toBeInTheDocument();
    expect(backend.turns()).toHaveLength(0);
  });

  it("at GATED (turns) uses the server's turn numbers", async () => {
    const backend = backendWith(
      usageView({ meter_state: "GATED", gate_reason: "TURNS_LIMIT", user_turns_used: 50 }),
    );
    render(<Chat api={backend.api} />);
    expect(
      await screen.findByText(
        "You used your 50 guest turns. Create a free account to continue and receive 100 turns every 30 days.",
      ),
    ).toBeInTheDocument();
  });

  it("keeps earlier replies readable when gated", async () => {
    const user = userEvent.setup();
    const gated = usageView({ meter_state: "GATED", gate_reason: "TURNS_LIMIT", user_turns_used: 50 });
    const backend = backendWith(usageView(), turnResponse({ usage: gated, allowance_exhausted: true }));
    render(<Chat api={backend.api} />);
    await user.type(screen.getByLabelText("Your message"), "dinner in Paris");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByText("Here is what I found nearby.")).toBeInTheDocument();
    expect(await screen.findByText(/You used your 50 guest turns/)).toBeInTheDocument();
    expect(screen.getByLabelText("Your message")).toBeDisabled();
    expect(screen.getByText("Here is what I found nearby.")).toBeInTheDocument();
  });

  it("updates the meter from TurnResponse.usage without another call", async () => {
    const user = userEvent.setup();
    const next = usageView({ meter_state: "QUIET_70", user_turns_used: 35 });
    const backend = backendWith(usageView(), turnResponse({ usage: next }));
    render(<Chat api={backend.api} />);
    await waitFor(() => expect(backend.requests.some((r) => r.url === "/v1/session:bootstrap")).toBe(true));
    await user.type(screen.getByLabelText("Your message"), "dinner in Paris");
    await user.click(screen.getByRole("button", { name: "Send" }));
    expect(await screen.findByLabelText("Usage")).toHaveTextContent("Turns 35 of 50");
    expect(backend.requests.filter((r) => r.url === "/v1/me/usage")).toHaveLength(0);
    expect(backend.requests.filter((r) => r.url === "/v1/session:bootstrap")).toHaveLength(1);
  });

  it("shows no meter and keeps chat usable when bootstrap fails", async () => {
    const backend = fakeBackend({
      "POST /v1/session:bootstrap": json(errorBody("POLICY"), 401),
      "POST /v1/agent/turns": json(turnResponse()),
    });
    render(<Chat api={backend.api} />);
    await waitFor(() => expect(backend.requests.some((r) => r.url === "/v1/session:bootstrap")).toBe(true));
    expect(screen.queryByLabelText("Usage")).toBeNull();
    expect(screen.getByLabelText("Your message")).toBeEnabled();
  });

  it("stores no usage in the browser", async () => {
    const backend = backendWith(usageView({ meter_state: "QUIET_70", user_turns_used: 35 }));
    render(<Chat api={backend.api} />);
    await screen.findByLabelText("Usage");
    expect(localStorage.length).toBe(0);
    expect(sessionStorage.length).toBe(0);
  });
});

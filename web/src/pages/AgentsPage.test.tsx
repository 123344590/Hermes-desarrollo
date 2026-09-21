// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { MemoryRouter } from "react-router";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  getAgents: vi.fn(),
  createAgent: vi.fn(),
  getAgentPermissions: vi.fn(),
  updateAgentPermissions: vi.fn(),
  rotateAgentToken: vi.fn(),
}));

vi.mock("@/lib/api", () => ({
  api: apiMocks,
  setManagementProfile: vi.fn(),
  getManagementProfile: vi.fn(() => ""),
}));

let container: HTMLDivElement;
let root: Root;
(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

async function waitFor(cond: () => boolean, timeoutMs = 5000) {
  const start = Date.now();
  while (!cond()) {
    if (Date.now() - start > timeoutMs) throw new Error("waitFor: condition never became true");
    await act(async () => {
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
  }
}

function click(el: Element | null) {
  if (!el) throw new Error("element not rendered");
  el.dispatchEvent(new MouseEvent("click", { bubbles: true, cancelable: true }));
}

function setInputValue(el: HTMLInputElement | null, value: string) {
  if (!el) throw new Error("input not rendered");
  Object.getOwnPropertyDescriptor(HTMLInputElement.prototype, "value")!.set!.call(el, value);
  el.dispatchEvent(new Event("input", { bubbles: true }));
}

const AGENT_FIXTURE = {
  name: "sales-bot",
  is_default: false,
  gateway_running: true,
  permissions: {
    webhooks: { can_manage: true, max: 3 },
    channels: { max: 2, allowed_platforms: ["telegram"] },
    skills: { policy: "read_write" as const, allowed: ["web_search"] },
    network: { allowed_ips: ["10.0.0.0/8"] },
  },
  crm: { url: "http://localhost:8642/p/sales-bot/v1", has_api_key: true },
};

async function renderAgentsPage() {
  const [{ default: AgentsPage }, { PageHeaderProvider }] = await Promise.all([
    import("./AgentsPage"),
    import("@/contexts/PageHeaderProvider"),
  ]);
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () =>
    root.render(
      <MemoryRouter>
        <PageHeaderProvider pluginTabs={[]}>
          <AgentsPage />
        </PageHeaderProvider>
      </MemoryRouter>,
    ),
  );
  await waitFor(() => document.body.textContent?.includes("sales-bot") === true);
}

beforeEach(() => {
  for (const fn of Object.values(apiMocks)) fn.mockReset();
  apiMocks.getAgents.mockResolvedValue({ agents: [AGENT_FIXTURE] });
});

afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
  vi.unstubAllGlobals();
});

describe("AgentsPage", () => {
  it("renders each agent's permission fields from the API response", async () => {
    await renderAgentsPage();

    const text = document.body.textContent ?? "";
    // Webhooks: can_manage + max render together.
    expect(text).toContain("manage (max 3)");
    // Channels max + allowed platforms.
    expect(text).toContain("max 2");
    expect(text).toContain("telegram");
    // Skills policy + allowed list.
    expect(text).toContain("read_write");
    expect(text).toContain("web_search");
    // Network allowlist.
    expect(text).toContain("10.0.0.0/8");
    // CRM url is shown, but the token is never present anywhere in the
    // list view — GET /api/admin/agents never returns one.
    expect(text).toContain("sales-bot/v1");
  });

  it("submits the permissions form to PUT with the flat AgentPermissionsUpdate shape", async () => {
    apiMocks.updateAgentPermissions.mockResolvedValue({
      webhooks: { can_manage: false, max: 0 },
      channels: { max: 5, allowed_platforms: ["telegram"] },
      skills: { policy: "read_write", allowed: ["web_search"] },
      network: { allowed_ips: ["10.0.0.0/8"] },
    });
    await renderAgentsPage();

    click(document.querySelector('button[aria-label="Permissions"]') ?? findButtonByText("Permissions"));
    await waitFor(() => document.querySelector('#channels-max') != null);

    // Change channels_max from 2 -> 5; every other field should round-trip
    // unchanged from the loaded permissions (webhooks_can_manage stays true
    // etc.) so the PUT body reflects a merge of "one edited field + the rest
    // of the loaded state", not a reset to defaults.
    setInputValue(document.querySelector<HTMLInputElement>("#channels-max"), "5");

    const saveButton = findButtonByText("Save permissions");
    click(saveButton);

    await waitFor(() => apiMocks.updateAgentPermissions.mock.calls.length > 0);

    expect(apiMocks.updateAgentPermissions).toHaveBeenCalledWith("sales-bot", {
      webhooks_can_manage: true,
      webhooks_max: 3,
      channels_max: 5,
      channels_allowed_platforms: ["telegram"],
      skills_policy: "read_write",
      skills_allowed: ["web_search"],
      network_allowed_ips: ["10.0.0.0/8"],
    });
  });
});

function findButtonByText(text: string): HTMLElement | null {
  return (
    Array.from(document.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) ??
    null
  );
}

// @vitest-environment jsdom
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

const apiMocks = vi.hoisted(() => ({
  listCustomEndpoints: vi.fn(),
  upsertCustomEndpoint: vi.fn(),
  activateCustomEndpoint: vi.fn(),
  deleteCustomEndpoint: vi.fn(),
  validateCustomEndpoint: vi.fn(),
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

function findButtonByText(text: string): HTMLElement | null {
  return (
    Array.from(document.querySelectorAll("button")).find((b) => b.textContent?.trim() === text) ??
    null
  );
}

const ENDPOINT_FIXTURE = {
  id: "my-proxy",
  name: "My Proxy",
  base_url: "https://proxy.example.com/v1",
  model: "gpt-4o-mini",
  models: ["gpt-4o-mini"],
  api_mode: "" as const,
  context_length: null,
  discover_models: true,
  has_api_key: true,
  api_key_preview: "sk-...abcd",
  is_current: true,
  source: "providers",
};

async function renderPanel() {
  const { CustomProvidersPanel } = await import("./CustomProvidersPanel");
  container = document.createElement("div");
  document.body.append(container);
  root = createRoot(container);
  await act(async () => root.render(<CustomProvidersPanel />));
}

beforeEach(() => {
  for (const fn of Object.values(apiMocks)) fn.mockReset();
  apiMocks.listCustomEndpoints.mockResolvedValue({
    endpoints: [ENDPOINT_FIXTURE],
    current: { provider: "my-proxy", model: "gpt-4o-mini", base_url: "https://proxy.example.com/v1" },
  });
});

afterEach(async () => {
  await act(async () => root?.unmount());
  container?.remove();
  vi.unstubAllGlobals();
});

describe("CustomProvidersPanel", () => {
  it("renders each configured endpoint's name, base URL, model, and main-model badge", async () => {
    await renderPanel();
    await waitFor(() => document.body.textContent?.includes("My Proxy") === true);

    const text = document.body.textContent ?? "";
    expect(text).toContain("My Proxy");
    expect(text).toContain("https://proxy.example.com/v1");
    expect(text).toContain("gpt-4o-mini");
    expect(text).toContain("main model");
  });

  it("submits the add-provider form with the upsert shape after a successful single-model validate", async () => {
    apiMocks.listCustomEndpoints.mockResolvedValue({
      endpoints: [],
      current: { provider: "", model: "", base_url: "" },
    });
    apiMocks.validateCustomEndpoint.mockResolvedValue({
      ok: true,
      reachable: true,
      message: "",
      models: ["llama-3-70b"],
      model_details: [],
      transport_checked: "chat_completions",
      resolved_base_url: "http://127.0.0.1:8080/v1",
    });
    apiMocks.upsertCustomEndpoint.mockResolvedValue({
      ok: true,
      id: "local-server",
      endpoints: [
        {
          ...ENDPOINT_FIXTURE,
          id: "local-server",
          name: "Local Server",
          base_url: "http://127.0.0.1:8080/v1",
          model: "llama-3-70b",
          models: ["llama-3-70b"],
        },
      ],
      current: { provider: "local-server", model: "llama-3-70b", base_url: "http://127.0.0.1:8080/v1" },
    });

    await renderPanel();
    await waitFor(() => document.body.textContent?.includes("No custom OpenAI-compatible") === true);

    click(findButtonByText("Add custom provider"));
    await waitFor(() => document.querySelector("#ce-name") != null);

    setInputValue(document.querySelector<HTMLInputElement>("#ce-name"), "Local Server");
    setInputValue(document.querySelector<HTMLInputElement>("#ce-base-url"), "http://127.0.0.1:8080/v1");

    click(findButtonByText("Test connection"));
    await waitFor(() => apiMocks.validateCustomEndpoint.mock.calls.length > 0);
    // Single detected model auto-selects, surfacing the Save button's enabled state.
    await waitFor(() => (findButtonByText("Save") as HTMLButtonElement | null)?.disabled === false);

    click(findButtonByText("Save"));
    await waitFor(() => apiMocks.upsertCustomEndpoint.mock.calls.length > 0);

    expect(apiMocks.upsertCustomEndpoint).toHaveBeenCalledWith({
      id: undefined,
      name: "Local Server",
      base_url: "http://127.0.0.1:8080/v1",
      model: "llama-3-70b",
      api_key: undefined,
      api_mode: "",
      context_length: undefined,
      discover_models: true,
      make_default: false,
      models: ["llama-3-70b"],
      model_details: undefined,
    });
  });
});

import { useCallback, useEffect, useState } from "react";
import { createPortal } from "react-dom";
import { Globe, Loader2, Plug, Plus, Star, Trash2, X, Zap } from "lucide-react";
import { api } from "@/lib/api";
import type {
  CustomEndpoint,
  CustomEndpointApiMode,
  CustomEndpointModelDetail,
  CustomEndpointUpdate,
  CustomEndpointValidationResponse,
} from "@/lib/api";
import { errorMessage } from "@/lib/api-error";
import { cn, themedBody } from "@/lib/utils";
import { useModalBehavior } from "@/hooks/useModalBehavior";
import { ConfirmDialog } from "@/components/ConfirmDialog";
import { Button } from "@nous-research/ui/ui/components/button";
import { Card, CardContent, CardHeader, CardTitle } from "@nous-research/ui/ui/components/card";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Checkbox } from "@nous-research/ui/ui/components/checkbox";
import { Select, SelectOption } from "@nous-research/ui/ui/components/select";
import { Spinner } from "@nous-research/ui/ui/components/spinner";

// Same choices as `hermes model`'s custom-provider setup (hermes_cli/model_setup_flows_custom.py);
// "" = runtime auto-detect from the URL.
const API_MODE_OPTIONS: readonly { value: CustomEndpointApiMode; label: string }[] = [
  { value: "", label: "Auto-detect" },
  { value: "chat_completions", label: "Chat Completions" },
  { value: "codex_responses", label: "Responses API" },
  { value: "anthropic_messages", label: "Anthropic Messages" },
];

interface EndpointForm {
  id: string;
  name: string;
  baseUrl: string;
  apiKey: string;
  apiMode: CustomEndpointApiMode;
  model: string;
  contextLength: string;
  discoverModels: boolean;
  makeDefault: boolean;
}

const EMPTY_FORM: EndpointForm = {
  id: "",
  name: "",
  baseUrl: "",
  apiKey: "",
  apiMode: "",
  model: "",
  contextLength: "",
  discoverModels: true,
  makeDefault: false,
};

/** Hostname-derived default name for Quick Connect, e.g. "https://litellm.acme.co/v1" ->
 * "litellm.acme.co". Falls back to the raw trimmed input when the URL doesn't parse yet
 * (still lets the user submit and hit the base_url validation error instead of a blank name). */
function deriveNameFromUrl(baseUrl: string): string {
  const trimmed = baseUrl.trim();
  try {
    const url = new URL(trimmed);
    return url.host || trimmed;
  } catch {
    return trimmed;
  }
}

/** Dedupe a candidate display name against already-configured endpoints. The backend keys
 * entries by a slug of `name` (`_custom_endpoint_id`) and MERGES onto a matching slug rather
 * than failing (see `_write_custom_endpoint`), so a Quick Connect that reused an existing
 * name would silently overwrite that endpoint instead of creating a new one. Compare
 * case-insensitively (the slug lowercases) and suffix "-2", "-3", ... until unique. */
function uniqueEndpointName(candidate: string, existing: CustomEndpoint[]): string {
  const taken = new Set(existing.map((e) => e.name.trim().toLowerCase()));
  if (!taken.has(candidate.toLowerCase())) return candidate;
  let n = 2;
  while (taken.has(`${candidate}-${n}`.toLowerCase())) n += 1;
  return `${candidate}-${n}`;
}

function formFromEndpoint(endpoint: CustomEndpoint): EndpointForm {
  return {
    id: endpoint.id,
    name: endpoint.name,
    baseUrl: endpoint.base_url,
    apiKey: "",
    apiMode: endpoint.api_mode ?? "",
    model: endpoint.model,
    contextLength: endpoint.context_length ? String(endpoint.context_length) : "",
    discoverModels: endpoint.discover_models,
    makeDefault: Boolean(endpoint.is_current),
  };
}

function toPayload(
  form: EndpointForm,
  models: string[],
  modelDetails: CustomEndpointModelDetail[],
): CustomEndpointUpdate {
  const contextLength = Number.parseInt(form.contextLength, 10);
  return {
    id: form.id.trim() || undefined,
    name: form.name.trim(),
    base_url: form.baseUrl.trim(),
    model: form.model.trim(),
    api_key: form.apiKey.trim() || undefined,
    api_mode: form.apiMode,
    context_length: Number.isFinite(contextLength) && contextLength > 0 ? contextLength : undefined,
    discover_models: form.discoverModels,
    make_default: form.makeDefault,
    models: models.length ? models : undefined,
    model_details: modelDetails.length ? modelDetails : undefined,
  };
}

/* ──────────────────────────────────────────────────────────────────── */
/*  Add / edit dialog — probe -> pick a detected model -> save            */
/* ──────────────────────────────────────────────────────────────────── */

interface CustomEndpointDialogProps {
  endpoint: CustomEndpoint | null;
  onClose(): void;
  onSaved(response: { endpoints: CustomEndpoint[] }): void;
}

function CustomEndpointDialog({ endpoint, onClose, onSaved }: CustomEndpointDialogProps) {
  const [form, setForm] = useState<EndpointForm>(endpoint ? formFromEndpoint(endpoint) : EMPTY_FORM);
  const [testing, setTesting] = useState(false);
  const [saving, setSaving] = useState(false);
  const [testResult, setTestResult] = useState<CustomEndpointValidationResponse | null>(null);
  const [discoveredModels, setDiscoveredModels] = useState<string[]>(endpoint?.models ?? []);
  // Alias metadata from the last Test; the backend resolves a picked alias to its
  // canonical model + reasoning effort on Save (#93622).
  const [discoveredDetails, setDiscoveredDetails] = useState<CustomEndpointModelDetail[]>([]);
  const [error, setError] = useState<string | null>(null);
  const modalRef = useModalBehavior({ open: true, onClose });

  // A saved endpoint is already known-reachable; only a fresh/edited URL needs
  // a green Test before Save is allowed (mirrors the CLI wizard's probe-first
  // sequencing, adapted for a form that can be reopened on existing data).
  const [verified, setVerified] = useState(Boolean(endpoint));

  const handleTest = async () => {
    setTesting(true);
    setError(null);
    try {
      const response = await api.validateCustomEndpoint(
        toPayload(form, discoveredModels, discoveredDetails),
      );
      setTestResult(response);
      setDiscoveredModels(response.models);
      setDiscoveredDetails(response.model_details ?? []);
      setVerified(response.ok);
      if (response.ok) {
        // Auto-select when exactly one model is detected (mirrors the CLI's
        // _pick_detected_model); otherwise leave the picker for the user.
        if (!form.model && response.models.length === 1) {
          setForm((cur) => ({ ...cur, model: response.models[0] }));
        }
        // Persist the URL that actually served /models: the runtime POSTs
        // {base_url}/chat/completions verbatim, so saving a typed root that
        // only worked via its /v1 variant would 404 every request (#65488).
        const resolved = response.resolved_base_url?.trim();
        if (resolved && resolved !== form.baseUrl.trim().replace(/\/+$/, "")) {
          setForm((cur) => ({ ...cur, baseUrl: resolved }));
        }
      }
    } catch (e) {
      setError(errorMessage(e));
      setVerified(false);
    } finally {
      setTesting(false);
    }
  };

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    try {
      const response = await api.upsertCustomEndpoint(
        toPayload(form, discoveredModels, discoveredDetails),
      );
      onSaved(response);
      onClose();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setSaving(false);
    }
  };

  const canTest = form.name.trim() && form.baseUrl.trim();
  const canSave = verified && form.name.trim() && form.baseUrl.trim() && form.model.trim();
  const modelChoices = Array.from(new Set([...discoveredModels, form.model].filter(Boolean)));

  return createPortal(
    <div
      ref={modalRef}
      className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 p-4"
      onClick={(e) => e.target === e.currentTarget && onClose()}
      role="dialog"
      aria-modal="true"
      aria-labelledby="custom-endpoint-title"
    >
      <div
        className={cn(
          themedBody,
          "relative flex max-h-[90vh] w-full max-w-lg flex-col border border-border bg-card shadow-2xl",
        )}
      >
        <Button
          ghost
          size="icon"
          onClick={onClose}
          className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
          aria-label="Close"
        >
          <X />
        </Button>

        <header className="p-5 pb-3 border-b border-border">
          <h2 id="custom-endpoint-title" className="font-mondwest text-display text-base tracking-wider">
            {endpoint ? "Edit custom provider" : "Add custom provider"}
          </h2>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto p-5 grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="ce-name">Name</Label>
            <Input
              id="ce-name"
              autoFocus
              placeholder="e.g. My Local Server"
              value={form.name}
              onChange={(e) => {
                setForm((cur) => ({ ...cur, name: e.target.value }));
                setVerified(Boolean(endpoint));
              }}
            />
          </div>

          <div className="grid gap-2">
            <Label htmlFor="ce-base-url">Base URL</Label>
            <Input
              id="ce-base-url"
              placeholder="https://api.example.com/v1"
              value={form.baseUrl}
              onChange={(e) => {
                setForm((cur) => ({ ...cur, baseUrl: e.target.value }));
                setVerified(false);
                setTestResult(null);
              }}
            />
          </div>

          <div className="grid gap-2">
            <Label htmlFor="ce-api-key">API key (optional)</Label>
            <Input
              id="ce-api-key"
              type="password"
              placeholder={endpoint ? "Leave blank to keep current key" : "Optional"}
              value={form.apiKey}
              onChange={(e) => {
                setForm((cur) => ({ ...cur, apiKey: e.target.value }));
                setVerified(false);
                setTestResult(null);
              }}
            />
          </div>

          <div className="flex flex-wrap gap-2">
            <Button
              type="button"
              outlined
              disabled={testing || !canTest}
              onClick={() => void handleTest()}
              prefix={testing ? <Spinner /> : <Zap className="h-3.5 w-3.5" />}
            >
              {testing ? "Testing…" : "Test connection"}
            </Button>
          </div>

          {testResult && (
            <div
              className={cn(
                "border px-3 py-2 text-xs",
                testResult.ok
                  ? "border-success/40 bg-success/10 text-success"
                  : testResult.reachable
                    ? "border-warning/40 bg-warning/10 text-warning"
                    : "border-destructive/40 bg-destructive/10 text-destructive",
              )}
            >
              {testResult.ok
                ? testResult.models.length
                  ? `Reachable — found ${testResult.models.length} model${testResult.models.length === 1 ? "" : "s"}.`
                  : "Reachable."
                : testResult.message || "Validation failed."}
            </div>
          )}

          {verified && (
            <>
              <fieldset className="grid min-w-0 gap-1.5">
                <Label>Model</Label>
                {modelChoices.length > 1 ? (
                  <Select
                    value={form.model}
                    onValueChange={(v) => setForm((cur) => ({ ...cur, model: v }))}
                  >
                    {modelChoices.map((m) => (
                      <SelectOption key={m} value={m}>
                        {m}
                      </SelectOption>
                    ))}
                  </Select>
                ) : (
                  <Input
                    placeholder="model-name"
                    value={form.model}
                    onChange={(e) => setForm((cur) => ({ ...cur, model: e.target.value }))}
                  />
                )}
                <p className="text-xs text-muted-foreground">
                  {modelChoices.length > 1
                    ? "Detected models — pick one, or type a different name above."
                    : "No models detected — type the model name to use."}
                </p>
              </fieldset>

              <div className="grid gap-3 sm:grid-cols-2">
                <fieldset className="grid min-w-0 gap-1.5">
                  <Label>API mode</Label>
                  <Select
                    value={form.apiMode}
                    onValueChange={(v) => setForm((cur) => ({ ...cur, apiMode: v as CustomEndpointApiMode }))}
                  >
                    {API_MODE_OPTIONS.map((opt) => (
                      <SelectOption key={opt.value || "auto"} value={opt.value}>
                        {opt.label}
                      </SelectOption>
                    ))}
                  </Select>
                </fieldset>

                <div className="grid gap-2">
                  <Label htmlFor="ce-context-length">Context length</Label>
                  <Input
                    id="ce-context-length"
                    inputMode="numeric"
                    placeholder="auto-detect"
                    value={form.contextLength}
                    onChange={(e) => setForm((cur) => ({ ...cur, contextLength: e.target.value }))}
                  />
                </div>
              </div>

              <div className="flex flex-wrap items-center gap-4">
                <div className="flex items-center gap-2.5">
                  <Checkbox
                    id="ce-discover-models"
                    checked={form.discoverModels}
                    onCheckedChange={(checked) =>
                      setForm((cur) => ({ ...cur, discoverModels: checked === true }))
                    }
                  />
                  <Label htmlFor="ce-discover-models" className="cursor-pointer text-sm">
                    Discover models
                  </Label>
                </div>

                <div className="flex items-center gap-2.5">
                  <Checkbox
                    id="ce-make-default"
                    checked={form.makeDefault}
                    onCheckedChange={(checked) =>
                      setForm((cur) => ({ ...cur, makeDefault: checked === true }))
                    }
                  />
                  <Label htmlFor="ce-make-default" className="cursor-pointer text-sm">
                    Set as main model
                  </Label>
                </div>
              </div>
            </>
          )}

          {error && <p className="text-xs text-destructive">{error}</p>}

          <div className="flex justify-end gap-2 pt-2">
            <Button type="button" ghost onClick={onClose} disabled={saving}>
              Cancel
            </Button>
            <Button
              type="button"
              onClick={() => void handleSave()}
              disabled={saving || !canSave}
              prefix={saving ? <Spinner /> : undefined}
            >
              {saving ? "Saving…" : "Save"}
            </Button>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/* ──────────────────────────────────────────────────────────────────── */
/*  Quick Connect — 2-field fast path (base URL + token) for the common   */
/*  case: a proxy like LiteLLM. Validate + save happen together on submit,*/
/*  with no separate "Test connection" click. Falls back to the full     */
/*  "Add custom provider" dialog ("Advanced") for API-mode overrides,     */
/*  context length, or picking among several detected models.            */
/* ──────────────────────────────────────────────────────────────────── */

interface QuickConnectDialogProps {
  existing: CustomEndpoint[];
  onClose(): void;
  onSaved(response: { endpoints: CustomEndpoint[] }): void;
  onAdvanced(): void;
}

function QuickConnectDialog({ existing, onClose, onSaved, onAdvanced }: QuickConnectDialogProps) {
  const [baseUrl, setBaseUrl] = useState("");
  const [apiKey, setApiKey] = useState("");
  const [connecting, setConnecting] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const modalRef = useModalBehavior({ open: true, onClose });

  const canConnect = baseUrl.trim().length > 0 && !connecting;

  const handleConnect = async () => {
    setConnecting(true);
    setError(null);
    try {
      const name = uniqueEndpointName(deriveNameFromUrl(baseUrl), existing);
      const probeForm: EndpointForm = {
        ...EMPTY_FORM,
        name,
        baseUrl: baseUrl.trim(),
        apiKey: apiKey.trim(),
      };

      // Validate first (no visible "Test connection" step) so a bad URL/token
      // surfaces inline instead of silently falling through to Save.
      const validation = await api.validateCustomEndpoint(toPayload(probeForm, [], []));
      if (!validation.ok) {
        setError(validation.message || "Could not connect — check the URL and token.");
        return;
      }

      // Single detected model auto-selects (mirrors the full form / CLI's
      // `_pick_detected_model`); with several, Quick Connect picks the first
      // one so the common single-model proxy case never needs an extra step —
      // multi-model nuance (or no detection at all) is what "Advanced" /
      // "Edit" afterward is for, rather than growing this dialog a picker.
      const models = validation.models;
      const modelDetails = validation.model_details ?? [];
      const model = models[0] ?? "";
      const resolvedBaseUrl = validation.resolved_base_url?.trim() || probeForm.baseUrl;

      const finalForm: EndpointForm = {
        ...probeForm,
        baseUrl: resolvedBaseUrl,
        model,
        discoverModels: true,
        makeDefault: true,
      };

      const response = await api.upsertCustomEndpoint(toPayload(finalForm, models, modelDetails));
      onSaved(response);
      onClose();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setConnecting(false);
    }
  };

  return createPortal(
    <div
      ref={modalRef}
      className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 p-4"
      onClick={(e) => e.target === e.currentTarget && onClose()}
      role="dialog"
      aria-modal="true"
      aria-labelledby="quick-connect-title"
    >
      <div
        className={cn(
          themedBody,
          "relative flex max-h-[90vh] w-full max-w-sm flex-col border border-border bg-card shadow-2xl",
        )}
      >
        <Button
          ghost
          size="icon"
          onClick={onClose}
          className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
          aria-label="Close"
        >
          <X />
        </Button>

        <header className="p-5 pb-3 border-b border-border">
          <h2 id="quick-connect-title" className="font-mondwest text-display text-base tracking-wider">
            Connect
          </h2>
          <p className="mt-1 text-xs text-muted-foreground">
            For a proxy like LiteLLM: base URL + token.
          </p>
        </header>

        <div className="min-h-0 flex-1 overflow-y-auto p-5 grid gap-4">
          <div className="grid gap-2">
            <Label htmlFor="qc-base-url">Base URL</Label>
            <Input
              id="qc-base-url"
              autoFocus
              placeholder="https://litellm.example.com/v1"
              value={baseUrl}
              onChange={(e) => {
                setBaseUrl(e.target.value);
                setError(null);
              }}
            />
          </div>

          <div className="grid gap-2">
            <Label htmlFor="qc-token">Token</Label>
            <Input
              id="qc-token"
              type="password"
              placeholder="Optional"
              value={apiKey}
              onChange={(e) => {
                setApiKey(e.target.value);
                setError(null);
              }}
            />
          </div>

          {error && <p className="text-xs text-destructive">{error}</p>}

          <div className="flex items-center justify-between gap-2 pt-2">
            <Button type="button" ghost size="sm" className="text-xs" onClick={onAdvanced} disabled={connecting}>
              Advanced setup…
            </Button>
            <div className="flex gap-2">
              <Button type="button" ghost onClick={onClose} disabled={connecting}>
                Cancel
              </Button>
              <Button
                type="button"
                onClick={() => void handleConnect()}
                disabled={!canConnect}
                prefix={connecting ? <Spinner /> : <Plug className="h-3.5 w-3.5" />}
              >
                {connecting ? "Connecting…" : "Connect"}
              </Button>
            </div>
          </div>
        </div>
      </div>
    </div>,
    document.body,
  );
}

/* ──────────────────────────────────────────────────────────────────── */
/*  Panel: list + row actions                                           */
/* ──────────────────────────────────────────────────────────────────── */

export function CustomProvidersPanel() {
  const [endpoints, setEndpoints] = useState<CustomEndpoint[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [dialogTarget, setDialogTarget] = useState<CustomEndpoint | "new" | null>(null);
  const [quickConnectOpen, setQuickConnectOpen] = useState(false);
  const [activatingId, setActivatingId] = useState<string | null>(null);
  const [pendingDelete, setPendingDelete] = useState<CustomEndpoint | null>(null);
  const [deleting, setDeleting] = useState(false);

  const load = useCallback(() => {
    setLoading(true);
    setError(null);
    api
      .listCustomEndpoints()
      .then((res) => setEndpoints(res.endpoints))
      .catch((e) => setError(errorMessage(e)))
      .finally(() => setLoading(false));
  }, []);

  useEffect(() => {
    load();
  }, [load]);

  const handleActivate = async (endpoint: CustomEndpoint) => {
    setActivatingId(endpoint.id);
    try {
      await api.activateCustomEndpoint(endpoint.id);
      load();
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setActivatingId(null);
    }
  };

  const handleDeleteConfirmed = async () => {
    if (!pendingDelete) return;
    setDeleting(true);
    try {
      const response = await api.deleteCustomEndpoint(pendingDelete.id);
      setEndpoints(response.endpoints);
      setPendingDelete(null);
    } catch (e) {
      setError(errorMessage(e));
    } finally {
      setDeleting(false);
    }
  };

  return (
    <Card className="min-w-0 max-w-full overflow-hidden">
      <CardHeader className="min-w-0 pb-3">
        <div className="flex min-w-0 flex-wrap items-center justify-between gap-2">
          <div className="flex items-center gap-2">
            <Globe className="h-4 w-4 shrink-0 text-muted-foreground" />
            <CardTitle className="text-sm">Custom Providers</CardTitle>
          </div>
          <div className="flex items-center gap-1.5">
            <Button
              size="sm"
              className="text-xs uppercase"
              prefix={<Plug className="h-3.5 w-3.5" />}
              onClick={() => setQuickConnectOpen(true)}
            >
              Connect
            </Button>
            <Button
              size="sm"
              outlined
              className="text-xs uppercase"
              prefix={<Plus className="h-3.5 w-3.5" />}
              onClick={() => setDialogTarget("new")}
            >
              Add custom provider
            </Button>
          </div>
        </div>
      </CardHeader>

      <CardContent className="min-w-0 space-y-2 pt-3">
        {loading && endpoints.length === 0 && (
          <div className="flex items-center justify-center py-6">
            <Spinner className="text-lg text-primary" />
          </div>
        )}

        {error && <p className="text-xs text-destructive">{error}</p>}

        {!loading && endpoints.length === 0 && !error && (
          <p className="text-xs text-text-secondary">
            No custom OpenAI-compatible providers configured yet.
          </p>
        )}

        {endpoints.map((endpoint) => (
          <div
            key={endpoint.id}
            className="flex min-w-0 flex-col gap-2 bg-muted/20 border border-border/50 px-3 py-2 sm:flex-row sm:items-center sm:justify-between sm:gap-3"
          >
            <div className="min-w-0 flex-1">
              <div className="flex items-center gap-2 flex-wrap">
                <span className="text-sm font-medium truncate">{endpoint.name}</span>
                {endpoint.is_current && (
                  <Badge tone="secondary" className="text-xs">
                    <Star className="h-2.5 w-2.5 mr-1" /> main model
                  </Badge>
                )}
              </div>
              <div className="text-xs font-mono text-text-secondary truncate">
                {endpoint.base_url}
              </div>
              <div className="text-xs text-text-tertiary truncate">
                {endpoint.model || "(no model set)"}
              </div>
            </div>

            <div className="flex items-center gap-1.5 shrink-0">
              <Button
                size="sm"
                outlined
                className="h-6 text-xs uppercase"
                disabled={endpoint.is_current || activatingId === endpoint.id}
                onClick={() => void handleActivate(endpoint)}
                prefix={activatingId === endpoint.id ? <Loader2 className="h-3 w-3 animate-spin" /> : undefined}
              >
                Activate
              </Button>
              <Button
                size="sm"
                ghost
                className="h-6 text-xs uppercase"
                onClick={() => setDialogTarget(endpoint)}
              >
                Edit
              </Button>
              <Button
                size="sm"
                ghost
                className="h-6 w-6 p-0 text-muted-foreground hover:text-destructive"
                aria-label={`Delete ${endpoint.name}`}
                onClick={() => setPendingDelete(endpoint)}
              >
                <Trash2 className="h-3.5 w-3.5" />
              </Button>
            </div>
          </div>
        ))}
      </CardContent>

      {quickConnectOpen && (
        <QuickConnectDialog
          existing={endpoints}
          onClose={() => setQuickConnectOpen(false)}
          onSaved={(response) => setEndpoints(response.endpoints)}
          onAdvanced={() => {
            setQuickConnectOpen(false);
            setDialogTarget("new");
          }}
        />
      )}

      {dialogTarget && (
        <CustomEndpointDialog
          endpoint={dialogTarget === "new" ? null : dialogTarget}
          onClose={() => setDialogTarget(null)}
          onSaved={(response) => setEndpoints(response.endpoints)}
        />
      )}

      <ConfirmDialog
        open={!!pendingDelete}
        title="Delete custom provider"
        description={
          pendingDelete
            ? `Delete "${pendingDelete.name}"? This removes it from providers and cannot be undone.`
            : undefined
        }
        destructive
        confirmLabel="Delete"
        loading={deleting}
        onCancel={() => setPendingDelete(null)}
        onConfirm={() => void handleDeleteConfirmed()}
      />
    </Card>
  );
}

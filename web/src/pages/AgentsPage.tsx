import { useCallback, useEffect, useLayoutEffect, useState } from "react";
import {
  Bot,
  Check,
  Copy,
  KeyRound,
  Plus,
  RotateCw,
  Shield,
  X,
} from "lucide-react";
import { Badge } from "@nous-research/ui/ui/components/badge";
import { Button } from "@nous-research/ui/ui/components/button";
import { Checkbox } from "@nous-research/ui/ui/components/checkbox";
import { Select, SelectOption } from "@nous-research/ui/ui/components/select";
import { Spinner } from "@nous-research/ui/ui/components/spinner";
import { H2 } from "@nous-research/ui/ui/components/typography/h2";
import { Card, CardContent } from "@nous-research/ui/ui/components/card";
import { Input } from "@nous-research/ui/ui/components/input";
import { Label } from "@nous-research/ui/ui/components/label";
import { Toast } from "@nous-research/ui/ui/components/toast";
import { useToast } from "@nous-research/ui/hooks/use-toast";
import { useModalBehavior } from "@/hooks/useModalBehavior";
import { api } from "@/lib/api";
import type { AdminAgentInfo, AgentPermissionsUpdate, MessagingPlatform } from "@/lib/api";
import { copyTextToClipboard } from "@/lib/clipboard";
import { usePageHeader } from "@/contexts/usePageHeader";
import { cn, themedBody } from "@/lib/utils";
import { errorMessage } from "@/lib/api-error";

// Mirrors hermes_cli/profiles.py::_PROFILE_ID_RE (see ProfilesPage.tsx) — a new
// agent IS a profile under the hood, so it is bound by the same name rule.
const AGENT_NAME_RE = /^[a-z0-9][a-z0-9_-]{0,63}$/;

const SKILLS_POLICIES: Array<{ value: AgentPermissionsUpdate["skills_policy"]; label: string }> = [
  { value: "read", label: "Read only" },
  { value: "read_write", label: "Read + write" },
  { value: "read_write_create", label: "Read + write + create" },
];

/** One-time reveal for a freshly issued CRM URL + Bearer token. The backend
 * never re-exposes the token after this response, so this is the only chance
 * the admin gets to copy it — mirrored here by never persisting it into any
 * state that survives closing the panel. */
function TokenReveal({
  title,
  url,
  token,
  onDone,
}: {
  title: string;
  url: string;
  token: string;
  onDone: () => void;
}) {
  return (
    <div className="grid gap-4">
      <p className="text-sm text-muted-foreground">
        Copy the URL and token now — the token is shown only once and cannot
        be retrieved again (only rotated).
      </p>

      <div className="grid gap-2">
        <Label>CRM URL</Label>
        <div className="flex items-center gap-2 border border-border bg-background/40 px-3 py-2">
          <span className="flex-1 min-w-0 truncate font-mono text-xs">{url}</span>
          <CopyIconButton value={url} />
        </div>
      </div>

      <div className="grid gap-2">
        <Label>Bearer token (shown once)</Label>
        <div className="flex items-center gap-2 border border-warning/40 bg-warning/10 px-3 py-2">
          <span className="flex-1 min-w-0 truncate font-mono text-xs">{token}</span>
          <CopyIconButton value={token} />
        </div>
      </div>

      <div className="flex justify-end">
        <Button className="uppercase" size="sm" onClick={onDone}>
          {title}
        </Button>
      </div>
    </div>
  );
}

/** A non-negative integer <Input type="number">. Renders 0 as an EMPTY field rather than the
 * literal "0" — a controlled `value={0}` makes the browser append keystrokes onto the visible
 * "0" (typing "1" produces "01") instead of replacing it. Parsing tolerates the empty string
 * (treated as 0) so the field can be fully cleared while editing. */
function NonNegativeIntInput({
  id,
  value,
  onChange,
}: {
  id: string;
  value: number;
  onChange: (next: number) => void;
}) {
  return (
    <Input
      id={id}
      type="number"
      min={0}
      value={value === 0 ? "" : value}
      onChange={(e) => {
        const raw = e.target.value;
        onChange(raw === "" ? 0 : Math.max(0, Math.trunc(Number(raw)) || 0));
      }}
    />
  );
}

function CopyIconButton({ value }: { value: string }) {
  const [copied, setCopied] = useState(false);
  const handleCopy = useCallback(() => {
    void copyTextToClipboard(value).then((ok) => {
      if (!ok) return;
      setCopied(true);
      window.setTimeout(() => setCopied(false), 1500);
    });
  }, [value]);
  return (
    <Button
      ghost
      size="icon"
      title="Copy"
      aria-label="Copy"
      onClick={handleCopy}
      className="text-muted-foreground hover:text-foreground"
    >
      {copied ? <Check /> : <Copy />}
    </Button>
  );
}

/** Local editable draft of one agent's permissions form. Kept separate from
 * the loaded AdminAgentInfo so edits don't mutate the list until saved. */
function permissionsToDraft(agent: AdminAgentInfo): AgentPermissionsUpdate {
  return {
    webhooks_can_manage: agent.permissions.webhooks.can_manage,
    webhooks_max: agent.permissions.webhooks.max,
    channels_max: agent.permissions.channels.max,
    channels_allowed_platforms: [...agent.permissions.channels.allowed_platforms],
    skills_policy: agent.permissions.skills.policy,
    skills_allowed: [...agent.permissions.skills.allowed],
    network_allowed_ips: [...agent.permissions.network.allowed_ips],
    network_allow_private_urls: agent.permissions.network.allow_private_urls,
    network_allowed_private_ips: [...agent.permissions.network.allowed_private_ips],
  };
}

interface PermissionsEditorProps {
  draft: AgentPermissionsUpdate;
  saving: boolean;
  availablePlatforms: MessagingPlatform[];
  onCancel: () => void;
  onChange: (next: AgentPermissionsUpdate) => void;
  onSave: () => void;
}

/** Inline editor for one agent's AgentPermissions — the shape PUT
 * /api/admin/agents/{name}/permissions accepts (admin_routes.py::_PermissionsBody). */
type OutboundMode = "deny" | "allow" | "scoped";

/** Derives the 3-way outbound-access radio state from the two underlying fields, so
 * "checked + non-empty allowlist" reads as one unambiguous mode instead of two booleans an
 * admin has to mentally combine.
 *
 * ``scopedSelected`` breaks the one genuine ambiguity: the moment an admin picks "Allow only
 * these IPs" but hasn't typed an IP yet, ``network_allowed_private_ips`` is still `[]` —
 * identical, on the two persisted fields alone, to plain "allow". Without this explicit
 * override the radio would silently re-derive back to "allow" on that first click and the
 * option would appear unselectable (the actual bug this override fixes). It is UI-only state,
 * never sent to the backend — an empty allowlist still means "no restriction" there regardless
 * of which radio was visually selected on the way to it. */
function outboundMode(
  draft: { network_allow_private_urls: boolean; network_allowed_private_ips: string[] },
  scopedSelected: boolean,
): OutboundMode {
  if (!draft.network_allow_private_urls) return "deny";
  if (draft.network_allowed_private_ips.length > 0) return "scoped";
  return scopedSelected ? "scoped" : "allow";
}

function PermissionsEditor({
  draft,
  saving,
  availablePlatforms,
  onCancel,
  onChange,
  onSave,
}: PermissionsEditorProps) {
  const csvToList = (v: string) =>
    v.split(",").map((s) => s.trim()).filter(Boolean);

  // See outboundMode()'s docstring: tracks an explicit "scoped" radio pick through the
  // window where the IP field is still empty, so the radio doesn't appear to un-select
  // itself on the very click that chose it.
  const [scopedSelected, setScopedSelected] = useState(
    draft.network_allow_private_urls && draft.network_allowed_private_ips.length > 0,
  );

  const togglePlatform = (id: string, checked: boolean) => {
    const next = checked
      ? [...draft.channels_allowed_platforms, id]
      : draft.channels_allowed_platforms.filter((p) => p !== id);
    onChange({ ...draft, channels_allowed_platforms: next });
  };

  return (
    <div className="grid gap-4 border-t border-border pt-4 mt-2">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div className="grid gap-2">
          <Label htmlFor="webhooks-max">Webhooks — max subscriptions</Label>
          <NonNegativeIntInput
            id="webhooks-max"
            value={draft.webhooks_max}
            onChange={(webhooks_max) => onChange({ ...draft, webhooks_max })}
          />
        </div>

        <div className="grid gap-2">
          <Label htmlFor="webhooks-can-manage">Webhooks — can manage</Label>
          <label
            className="flex items-center gap-2 text-sm text-muted-foreground h-9"
            htmlFor="webhooks-can-manage"
          >
            <input
              id="webhooks-can-manage"
              type="checkbox"
              checked={draft.webhooks_can_manage}
              onChange={(e) => onChange({ ...draft, webhooks_can_manage: e.target.checked })}
            />
            Allow creating/deleting its own webhook subscriptions
          </label>
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div className="grid gap-2">
          <Label htmlFor="channels-max">Channels — max connected</Label>
          <NonNegativeIntInput
            id="channels-max"
            value={draft.channels_max}
            onChange={(channels_max) => onChange({ ...draft, channels_max })}
          />
        </div>

        <div className="grid gap-2">
          <Label id="channels-platforms-label">Channels — allowed platforms</Label>
          <div
            className="grid grid-cols-2 gap-x-3 gap-y-1.5 border border-border bg-background/40 px-3 py-2 max-h-40 overflow-y-auto"
            role="group"
            aria-labelledby="channels-platforms-label"
          >
            {availablePlatforms.length === 0 && (
              <span className="col-span-2 text-xs text-muted-foreground">
                No platform catalog available.
              </span>
            )}
            {availablePlatforms.map((platform) => (
              <label
                key={platform.id}
                htmlFor={`channels-platform-${platform.id}`}
                className="flex items-center gap-2 text-sm text-muted-foreground"
              >
                <Checkbox
                  id={`channels-platform-${platform.id}`}
                  checked={draft.channels_allowed_platforms.includes(platform.id)}
                  onCheckedChange={(checked) => togglePlatform(platform.id, checked === true)}
                />
                {platform.name}
              </label>
            ))}
          </div>
        </div>
      </div>

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div className="grid gap-2">
          <Label htmlFor="skills-policy">Skills policy</Label>
          <Select
            id="skills-policy"
            value={draft.skills_policy}
            onValueChange={(v) =>
              onChange({ ...draft, skills_policy: v as AgentPermissionsUpdate["skills_policy"] })
            }
          >
            {SKILLS_POLICIES.map((p) => (
              <SelectOption key={p.value} value={p.value}>
                {p.label}
              </SelectOption>
            ))}
          </Select>
        </div>

        <div className="grid gap-2">
          <Label htmlFor="skills-allowed">Skills — allowed (empty = all)</Label>
          <Input
            id="skills-allowed"
            placeholder="comma-separated skill names"
            value={draft.skills_allowed.join(", ")}
            onChange={(e) => onChange({ ...draft, skills_allowed: csvToList(e.target.value) })}
          />
        </div>
      </div>

      <div className="grid gap-2">
        <Label htmlFor="network-ips">
          Network — restrict incoming CRM requests to these IPs (empty = unrestricted)
        </Label>
        <Input
          id="network-ips"
          placeholder="comma-separated IPs or CIDR ranges"
          value={draft.network_allowed_ips.join(", ")}
          onChange={(e) => onChange({ ...draft, network_allowed_ips: csvToList(e.target.value) })}
        />
        <p className="text-xs text-muted-foreground">
          Allowlists the source IP of INBOUND requests to this agent's own CRM endpoint
          (<code>/p/{"<name>"}/v1</code>). Does not affect this agent's own outbound network
          access — see below.
        </p>
      </div>

      <div className="grid gap-2 border-t border-border pt-4">
        <Label>Network — outbound private access</Label>
        <p className="text-xs text-muted-foreground">
          OUTBOUND: what this agent's own tools (terminal, URL fetch, browser) may reach out
          to on private/internal networks — not who may reach IT (that's the inbound CRM
          allowlist above). Cloud metadata addresses (e.g. 169.254.169.254) stay blocked no
          matter which option is chosen.
        </p>

        <div className="grid gap-2">
          {(
            [
              {
                value: "deny",
                label: "Deny all — no private/internal IPs reachable",
              },
              {
                value: "scoped",
                label: "Allow only these IPs/CIDRs",
              },
              {
                value: "allow",
                label: "Allow all private/internal IPs",
              },
            ] as const
          ).map((opt) => (
            <label
              key={opt.value}
              className="flex items-center gap-2 text-sm text-muted-foreground"
              htmlFor={`network-outbound-${opt.value}`}
            >
              <input
                id={`network-outbound-${opt.value}`}
                type="radio"
                name="network-outbound-mode"
                checked={outboundMode(draft, scopedSelected) === opt.value}
                onChange={() => {
                  setScopedSelected(opt.value === "scoped");
                  if (opt.value === "deny") {
                    onChange({
                      ...draft,
                      network_allow_private_urls: false,
                      network_allowed_private_ips: [],
                    });
                  } else if (opt.value === "allow") {
                    onChange({
                      ...draft,
                      network_allow_private_urls: true,
                      network_allowed_private_ips: [],
                    });
                  } else {
                    onChange({ ...draft, network_allow_private_urls: true });
                  }
                }}
              />
              {opt.label}
            </label>
          ))}
        </div>

        {outboundMode(draft, scopedSelected) === "scoped" && (
          <div className="grid gap-2 pl-6">
            <Label htmlFor="network-allowed-private-ips">
              Allowed private IPs/CIDRs
            </Label>
            <Input
              id="network-allowed-private-ips"
              placeholder="comma-separated IPs or CIDR ranges, e.g. 10.147.200.3/32"
              value={draft.network_allowed_private_ips.join(", ")}
              onChange={(e) =>
                onChange({
                  ...draft,
                  network_allowed_private_ips: csvToList(e.target.value),
                })
              }
            />
            <p className="text-xs text-muted-foreground">
              Only these exact ranges are reachable — e.g. one internal CRM host rather than
              the whole private network. At least one entry is required for this mode to take
              effect; an empty list here behaves like "Allow all" until you add one.
            </p>
          </div>
        )}
      </div>

      <div className="flex justify-end gap-2">
        <Button size="sm" ghost onClick={onCancel} disabled={saving}>
          Cancel
        </Button>
        <Button
          size="sm"
          className="uppercase"
          onClick={onSave}
          disabled={saving}
          prefix={saving ? <Spinner /> : undefined}
        >
          {saving ? "Saving…" : "Save permissions"}
        </Button>
      </div>
    </div>
  );
}

export default function AgentsPage() {
  const [agents, setAgents] = useState<AdminAgentInfo[]>([]);
  const [loading, setLoading] = useState(true);
  // null = the caller isn't an admin (403) — degrade to a clear message
  // rather than an empty/broken list, matching how the rest of the
  // dashboard surfaces a permission error via errorMessage()/ApiError
  // instead of crashing on a raw response.
  const [forbidden, setForbidden] = useState(false);
  const { toast, showToast } = useToast();
  const { setEnd } = usePageHeader();

  const [editingFor, setEditingFor] = useState<string | null>(null);
  const [draft, setDraft] = useState<AgentPermissionsUpdate | null>(null);
  const [savingPerms, setSavingPerms] = useState(false);
  // Canonical platform catalog for the "Channels — allowed platforms" multi-select — the same
  // /api/messaging/platforms list ChannelsPage.tsx renders, so an agent's allowlist can only ever
  // reference a platform Hermes actually supports (no more hand-typed CSV that silently typos).
  const [availablePlatforms, setAvailablePlatforms] = useState<MessagingPlatform[]>([]);

  const [rotatingFor, setRotatingFor] = useState<string | null>(null);
  // The one-time reveal after create or rotate. `mode: "create"` also needs
  // a follow-up list refresh once dismissed (the new agent isn't in `agents` yet).
  const [reveal, setReveal] = useState<
    { mode: "create" | "rotate"; name: string; url: string; token: string } | null
  >(null);

  const [createModalOpen, setCreateModalOpen] = useState(false);
  const [newName, setNewName] = useState("");
  const [newDescription, setNewDescription] = useState("");
  const [creating, setCreating] = useState(false);
  const closeCreateModal = useCallback(() => setCreateModalOpen(false), []);
  const createModalRef = useModalBehavior({ open: createModalOpen, onClose: closeCreateModal });
  const revealModalRef = useModalBehavior({ open: reveal != null, onClose: () => {} });

  const load = useCallback(() => {
    api
      .getAgents()
      .then((res) => {
        setForbidden(false);
        setAgents(res.agents);
      })
      .catch((e) => {
        if (e && typeof e === "object" && "status" in e && (e as { status: number }).status === 403) {
          setForbidden(true);
          return;
        }
        showToast(`Failed to load agents: ${errorMessage(e)}`, "error");
      })
      .finally(() => setLoading(false));
  }, [showToast]);

  useEffect(() => {
    load();
  }, [load]);

  useEffect(() => {
    api
      .getMessagingPlatforms()
      .then((res) => setAvailablePlatforms(res.platforms))
      .catch(() => {
        // Non-fatal: the multi-select just renders empty ("Loading platforms…" never resolves)
        // rather than blocking the whole page over a catalog fetch failure.
      });
  }, []);

  const handleCreate = async () => {
    const name = newName.trim();
    if (!name) {
      showToast("Name required", "error");
      return;
    }
    if (!AGENT_NAME_RE.test(name)) {
      showToast("Invalid name: lowercase letters, numbers, - and _ only", "error");
      return;
    }
    setCreating(true);
    try {
      const res = await api.createAgent({ name, description: newDescription.trim() || undefined });
      showToast(`Agent created: ${name}`, "success");
      setNewName("");
      setNewDescription("");
      setCreateModalOpen(false);
      setReveal({ mode: "create", name: res.crm.name, url: res.crm.url, token: res.crm.token });
    } catch (e) {
      showToast(`Failed to create agent: ${errorMessage(e)}`, "error");
    } finally {
      setCreating(false);
    }
  };

  const openEditor = (agent: AdminAgentInfo) => {
    if (editingFor === agent.name) {
      setEditingFor(null);
      setDraft(null);
      return;
    }
    setEditingFor(agent.name);
    setDraft(permissionsToDraft(agent));
  };

  const handleSavePermissions = async () => {
    if (!editingFor || !draft) return;
    setSavingPerms(true);
    try {
      const updated = await api.updateAgentPermissions(editingFor, draft);
      setAgents((prev) =>
        prev.map((a) => (a.name === editingFor ? { ...a, permissions: updated } : a)),
      );
      showToast(`Permissions saved: ${editingFor}`, "success");
      setEditingFor(null);
      setDraft(null);
    } catch (e) {
      showToast(`Failed to save permissions: ${errorMessage(e)}`, "error");
    } finally {
      setSavingPerms(false);
    }
  };

  const handleRotate = async (name: string) => {
    setRotatingFor(name);
    try {
      const res = await api.rotateAgentToken(name);
      showToast(`Token rotated: ${name}`, "success");
      setReveal({ mode: "rotate", name: res.name, url: res.url, token: res.token });
    } catch (e) {
      showToast(`Failed to rotate token: ${errorMessage(e)}`, "error");
    } finally {
      setRotatingFor(null);
    }
  };

  const closeReveal = () => {
    const wasCreate = reveal?.mode === "create";
    setReveal(null);
    if (wasCreate) load();
    else load(); // has_api_key flips true after rotate too; keep the card in sync.
  };

  useLayoutEffect(() => {
    setEnd(
      <Button
        className="uppercase"
        size="sm"
        prefix={<Plus />}
        onClick={() => setCreateModalOpen(true)}
        disabled={forbidden}
      >
        New agent
      </Button>,
    );
    return () => setEnd(null);
  }, [setEnd, loading, forbidden]);

  if (loading) {
    return (
      <div className="flex items-center justify-center py-24">
        <Spinner className="text-2xl text-primary" />
      </div>
    );
  }

  if (forbidden) {
    return (
      <div className="flex flex-col gap-6">
        <Toast toast={toast} />
        <Card className="border-warning/50">
          <CardContent className="flex items-start gap-3 py-6 text-sm">
            <Shield className="h-5 w-5 shrink-0 text-warning" />
            <div className="flex flex-col gap-1">
              <span className="font-medium">Admin access required</span>
              <span className="text-muted-foreground">
                Agent permissions and CRM tokens are only manageable from the
                dashboard's admin account. Sign in as the admin to view this
                page.
              </span>
            </div>
          </CardContent>
        </Card>
      </div>
    );
  }

  return (
    <div className="flex flex-col gap-6">
      <Toast toast={toast} />

      {/* Create agent modal */}
      {createModalOpen && (
        <div
          ref={createModalRef}
          className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 p-4"
          onClick={(e) => e.target === e.currentTarget && closeCreateModal()}
          role="dialog"
          aria-modal="true"
          aria-labelledby="create-agent-title"
        >
          <div className={cn(themedBody, "relative w-full max-w-md border border-border bg-card shadow-2xl flex flex-col max-h-[90vh]")}>
            <Button
              ghost
              size="icon"
              onClick={closeCreateModal}
              className="absolute right-2 top-2 text-muted-foreground hover:text-foreground"
              aria-label="Close"
            >
              <X />
            </Button>

            <header className="p-5 pb-3 border-b border-border">
              <h2 id="create-agent-title" className="font-mondwest text-display text-base tracking-wider">
                New agent
              </h2>
            </header>

            <div className="min-h-0 overflow-y-auto p-5 grid gap-4">
              <div className="grid gap-2">
                <Label htmlFor="agent-name">Name</Label>
                <Input
                  id="agent-name"
                  autoFocus
                  placeholder="e.g. sales-bot"
                  value={newName}
                  onChange={(e) => setNewName(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") handleCreate();
                  }}
                  aria-invalid={newName.trim() !== "" && !AGENT_NAME_RE.test(newName.trim())}
                />
                <p className="text-xs text-muted-foreground">
                  Lowercase letters, numbers, - and _ only.
                </p>
              </div>

              <div className="grid gap-2">
                <Label htmlFor="agent-description">Description (optional)</Label>
                <textarea
                  id="agent-description"
                  className="flex min-h-[64px] w-full border border-input bg-transparent px-3 py-2 text-sm shadow-sm placeholder:text-muted-foreground focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring"
                  placeholder="What is this agent for?"
                  value={newDescription}
                  onChange={(e) => setNewDescription(e.target.value)}
                />
              </div>

              <p className="text-xs text-muted-foreground">
                New agents start fail-closed: no webhooks, no channels, read-only
                skills, no network restriction until you grant capabilities below.
              </p>

              <div className="flex justify-end">
                <Button className="uppercase" size="sm" onClick={handleCreate} disabled={creating}>
                  {creating ? "Creating…" : "Create"}
                </Button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* One-time token reveal — create or rotate */}
      {reveal && (
        <div
          ref={revealModalRef}
          className="fixed inset-0 z-[100] flex items-center justify-center bg-background/85 p-4"
          role="dialog"
          aria-modal="true"
          aria-labelledby="reveal-token-title"
        >
          <div className={cn(themedBody, "relative w-full max-w-lg border border-border bg-card shadow-2xl flex flex-col max-h-[90vh]")}>
            <header className="p-5 pb-3 border-b border-border">
              <h2 id="reveal-token-title" className="font-mondwest text-display text-base tracking-wider">
                {reveal.mode === "create" ? "Agent created" : "Token rotated"}
                <span className="text-muted-foreground"> · {reveal.name}</span>
              </h2>
            </header>

            <div className="p-5">
              <TokenReveal
                title="Done"
                url={reveal.url}
                token={reveal.token}
                onDone={closeReveal}
              />
            </div>
          </div>
        </div>
      )}

      <div className="flex flex-col gap-3">
        <H2 variant="sm" className="flex items-center gap-2 text-muted-foreground">
          <Bot className="h-4 w-4" />
          Agents ({agents.length})
        </H2>

        <p className="text-xs text-muted-foreground -mt-1">
          Each agent is a profile with its own CRM URL, API token, and
          capability grants. New agents are born fail-closed.
        </p>

        {agents.length === 0 && (
          <Card>
            <CardContent className="py-8 text-center text-sm text-muted-foreground">
              No agents yet.
            </CardContent>
          </Card>
        )}

        <div className="grid grid-cols-1 gap-3 xl:grid-cols-2">
          {agents.map((agent) => {
            const isEditing = editingFor === agent.name;
            const p = agent.permissions;
            return (
              <Card key={agent.name}>
                <CardContent className="flex flex-col gap-3 py-4">
                  <div className="flex items-start justify-between gap-2">
                    <div className="flex min-w-0 flex-1 flex-wrap items-center gap-1.5">
                      <span className="font-medium text-sm truncate">{agent.name}</span>
                      {agent.is_default && <Badge tone="secondary">default</Badge>}
                      <Badge
                        tone={agent.gateway_running ? "success" : "outline"}
                      >
                        {agent.gateway_running ? "gateway running" : "gateway stopped"}
                      </Badge>
                      <Badge tone={agent.crm.has_api_key ? "success" : "warning"}>
                        {agent.crm.has_api_key ? "token set" : "no token"}
                      </Badge>
                    </div>

                    <div className="flex items-center gap-1 shrink-0">
                      <Button
                        ghost
                        size="sm"
                        className="uppercase"
                        onClick={() => openEditor(agent)}
                      >
                        {isEditing ? "Close" : "Permissions"}
                      </Button>
                      <Button
                        ghost
                        size="icon"
                        title="Rotate token"
                        aria-label="Rotate token"
                        disabled={rotatingFor === agent.name}
                        onClick={() => handleRotate(agent.name)}
                      >
                        {rotatingFor === agent.name ? (
                          <Spinner />
                        ) : (
                          <RotateCw className="h-4 w-4" />
                        )}
                      </Button>
                    </div>
                  </div>

                  <div className="flex items-center gap-2 text-xs text-muted-foreground">
                    <KeyRound className="h-3.5 w-3.5 shrink-0" />
                    <span className="flex-1 min-w-0 truncate font-mono">{agent.crm.url}</span>
                    <CopyIconButton value={agent.crm.url} />
                  </div>

                  <div className="grid grid-cols-2 gap-x-4 gap-y-1 text-xs text-muted-foreground">
                    <span>
                      Webhooks: {p.webhooks.can_manage ? `manage (max ${p.webhooks.max})` : "denied"}
                    </span>
                    <span>Channels: max {p.channels.max}</span>
                    <span className="col-span-2 truncate">
                      Platforms: {p.channels.allowed_platforms.length ? p.channels.allowed_platforms.join(", ") : "(none)"}
                    </span>
                    <span className="col-span-2">Skills policy: {p.skills.policy}</span>
                    <span className="col-span-2 truncate">
                      Skills allowed: {p.skills.allowed.length ? p.skills.allowed.join(", ") : "(all)"}
                    </span>
                    <span className="col-span-2 truncate" title="Source IPs allowed to call this agent's CRM endpoint">
                      Inbound CRM access: {p.network.allowed_ips.length ? p.network.allowed_ips.join(", ") : "(unrestricted)"}
                    </span>
                    <span
                      className="col-span-2 truncate"
                      title="What this agent's own outbound tools (terminal, URL fetch, browser) may reach on private networks"
                    >
                      Outbound private access:{" "}
                      {p.network.allow_private_urls
                        ? p.network.allowed_private_ips.length
                          ? `restricted to ${p.network.allowed_private_ips.join(", ")}`
                          : "any private IP"
                        : "denied"}
                    </span>
                  </div>

                  {isEditing && draft && (
                    <PermissionsEditor
                      draft={draft}
                      saving={savingPerms}
                      availablePlatforms={availablePlatforms}
                      onCancel={() => {
                        setEditingFor(null);
                        setDraft(null);
                      }}
                      onChange={setDraft}
                      onSave={handleSavePermissions}
                    />
                  )}
                </CardContent>
              </Card>
            );
          })}
        </div>
      </div>
    </div>
  );
}

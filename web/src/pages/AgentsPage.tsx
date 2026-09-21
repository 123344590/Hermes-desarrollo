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
import type { AdminAgentInfo, AgentPermissionsUpdate } from "@/lib/api";
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
  };
}

interface PermissionsEditorProps {
  draft: AgentPermissionsUpdate;
  saving: boolean;
  onCancel: () => void;
  onChange: (next: AgentPermissionsUpdate) => void;
  onSave: () => void;
}

/** Inline editor for one agent's AgentPermissions — the shape PUT
 * /api/admin/agents/{name}/permissions accepts (admin_routes.py::_PermissionsBody). */
function PermissionsEditor({ draft, saving, onCancel, onChange, onSave }: PermissionsEditorProps) {
  const csvToList = (v: string) =>
    v.split(",").map((s) => s.trim()).filter(Boolean);

  return (
    <div className="grid gap-4 border-t border-border pt-4 mt-2">
      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4">
        <div className="grid gap-2">
          <Label htmlFor="webhooks-max">Webhooks — max subscriptions</Label>
          <Input
            id="webhooks-max"
            type="number"
            min={0}
            value={draft.webhooks_max}
            onChange={(e) =>
              onChange({ ...draft, webhooks_max: Math.max(0, Number(e.target.value) || 0) })
            }
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
          <Input
            id="channels-max"
            type="number"
            min={0}
            value={draft.channels_max}
            onChange={(e) =>
              onChange({ ...draft, channels_max: Math.max(0, Number(e.target.value) || 0) })
            }
          />
        </div>

        <div className="grid gap-2">
          <Label htmlFor="channels-platforms">Channels — allowed platforms</Label>
          <Input
            id="channels-platforms"
            placeholder="comma-separated, e.g. telegram,discord"
            value={draft.channels_allowed_platforms.join(", ")}
            onChange={(e) =>
              onChange({ ...draft, channels_allowed_platforms: csvToList(e.target.value) })
            }
          />
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
        <Label htmlFor="network-ips">Network — allowed IPs (empty = unrestricted)</Label>
        <Input
          id="network-ips"
          placeholder="comma-separated IPs or CIDR ranges"
          value={draft.network_allowed_ips.join(", ")}
          onChange={(e) => onChange({ ...draft, network_allowed_ips: csvToList(e.target.value) })}
        />
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
                    <span className="col-span-2 truncate">
                      Network: {p.network.allowed_ips.length ? p.network.allowed_ips.join(", ") : "(unrestricted)"}
                    </span>
                  </div>

                  {isEditing && draft && (
                    <PermissionsEditor
                      draft={draft}
                      saving={savingPerms}
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

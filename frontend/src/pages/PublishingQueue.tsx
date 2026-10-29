import { useState } from "react";
import { api } from "../api";
import type { PublishJob } from "../types";
import { errText, useData } from "../util";

const PLATFORM_NAMES: Record<string, string> = { youtube: "YouTube", tiktok: "TikTok" };
const ACTIVE = new Set(["queued", "uploading"]);
const BAD = new Set(["failed", "needs_action"]);

export default function PublishingQueuePage() {
  const [status, setStatus] = useState("");
  const jobs = useData(() => api.publishJobs({ status: status || undefined }), [status], 2000);
  const [err, setErr] = useState<string | null>(null);
  const act = async (fn: () => Promise<unknown>) => {
    setErr(null);
    try { await fn(); jobs.refresh(); } catch (e) { setErr(errText(e)); }
  };

  return (
    <div className="col" style={{ gap: 14 }}>
      <div className="row between">
        <h1>Publishing queue</h1>
        <select value={status} onChange={(e) => setStatus(e.target.value)}>
          <option value="">all</option>
          <option value="queued,uploading">active</option>
          <option value="failed,needs_action">needs attention</option>
          <option value="uploaded,draft,published">done</option>
        </select>
      </div>
      {err && <div className="err small">{err}</div>}
      <div className="panel" style={{ padding: 0 }}>
        <table>
          <thead>
            <tr><th>Platform</th><th>Account</th><th>Title</th><th>Status</th><th>Updated</th><th /></tr>
          </thead>
          <tbody>
            {(jobs.data ?? []).map((p) => <Row key={p.id} p={p} act={act} />)}
          </tbody>
        </table>
        {jobs.data?.length === 0 && (
          <div className="empty">
            Nothing here yet. Publish a rendered clip from the review page, or{" "}
            <a href="#/settings">connect an account</a> first.
          </div>
        )}
      </div>
    </div>
  );
}

function Row({ p, act }: { p: PublishJob; act: (fn: () => Promise<unknown>) => void }) {
  return (
    <tr>
      <td>{PLATFORM_NAMES[p.platform] ?? p.platform}</td>
      <td>{p.account_label}</td>
      <td className="small">
        {p.title || <span className="muted">untitled</span>}
        {(p.error || p.last_operation) && <div className="muted">{p.error || p.last_operation}</div>}
      </td>
      <td>
        <span className={`chip ${BAD.has(p.status) ? "failed" : ACTIVE.has(p.status) ? "running" : "completed"}`}>
          {p.status}
        </span>
        {p.platform_post_url && (
          <> <a href={p.platform_post_url} target="_blank" rel="noreferrer">open</a></>
        )}
      </td>
      <td className="small muted">{new Date(p.updated_at).toLocaleString()}</td>
      <td>
        {ACTIVE.has(p.status) && (
          <button className="tiny bad" onClick={() => act(() => api.cancelPublish(p.id))}>Cancel</button>
        )}
        {BAD.has(p.status) && (
          <button className="tiny" onClick={() => act(() => api.retryPublish(p.id))}>Retry</button>
        )}
      </td>
    </tr>
  );
}

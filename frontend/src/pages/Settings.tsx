import { useEffect, useState } from "react";
import { api } from "../api";
import ConfirmButton from "../components/ConfirmButton";
import type { Prefs } from "../types";
import { errText, go, useData, useHashRoute } from "../util";

const PLATFORM_NAMES: Record<string, string> = { youtube: "YouTube", tiktok: "TikTok" };

const MODELS = ["tiny", "base", "small", "medium", "large-v3", "large-v3-turbo", "distil-large-v3",
  "tiny.en", "base.en", "small.en", "medium.en"];

export default function SettingsPage() {
  const prefs = useData(() => api.settings(), []);
  const [diagRefresh, setDiagRefresh] = useState(0);
  const diag = useData(() => api.diagnostics(diagRefresh > 0), [diagRefresh]);
  const [form, setForm] = useState<Prefs | null>(null);
  const [msg, setMsg] = useState<string | null>(null);
  const [err, setErr] = useState<string | null>(null);
  useEffect(() => { if (prefs.data) setForm(prefs.data); }, [prefs.data]);

  const route = useHashRoute();
  const platforms = useData(() => api.publishPlatforms(), []);
  const accounts = useData(() => api.publishAccounts(), []);
  const [connectMsg, setConnectMsg] = useState<{ ok: boolean; text: string } | null>(null);
  useEffect(() => {
    const [, kind, value] = route;
    if (kind === "publish-ok") {
      const [platform] = (value ?? "").split(":");
      setConnectMsg({ ok: true, text: `Connected ${PLATFORM_NAMES[platform] ?? platform}.` });
      accounts.refresh();
      go("/settings");
    } else if (kind === "publish-err") {
      setConnectMsg({ ok: false, text: `Couldn't connect: ${value}` });
      go("/settings");
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [route[1], route[2]]);

  if (!form) return <div className="muted">Loading…</div>;
  const set = <K extends keyof Prefs>(k: K, v: Prefs[K]) => setForm({ ...form, [k]: v });
  const save = async () => {
    setErr(null);
    setMsg(null);
    try { setForm(await api.saveSettings(form)); setMsg("Saved."); } catch (e) { setErr(errText(e)); }
  };
  const d = diag.data;
  const size = `${form.output_width}x${form.output_height}`;
  const connect = async (platform: string) => {
    try {
      const { authorize_url } = await api.publishConnect(platform);
      window.location.href = authorize_url;
    } catch (e) {
      setConnectMsg({ ok: false, text: errText(e) });
    }
  };

  return (
    <div className="col" style={{ gap: 18, maxWidth: 900 }}>
      <h1>Settings</h1>
      <div className="panel col">
        <h3>Transcription (local, faster-whisper)</h3>
        <div className="row">
          <label>Engine
            <select value={form.transcription_engine ?? ""} onChange={(e) => set("transcription_engine", e.target.value || null)}>
              <option value="">default (faster-whisper)</option>
              <option value="faster-whisper">faster-whisper</option>
              <option value="sidecar">sidecar files only (.srt/.vtt/.json next to video)</option>
            </select>
          </label>
          <label>Model
            <select value={form.whisper_model} onChange={(e) => set("whisper_model", e.target.value)}>
              {MODELS.map((m) => <option key={m} value={m}>{m}</option>)}
            </select>
          </label>
          <label>Device
            <select value={form.whisper_device} onChange={(e) => set("whisper_device", e.target.value as Prefs["whisper_device"])}>
              <option value="auto">auto (GPU if available)</option><option value="cuda">GPU (CUDA)</option><option value="cpu">CPU</option>
            </select>
          </label>
          <label>Language
            <input value={form.whisper_language ?? ""} placeholder="auto-detect" style={{ width: 110 }}
                   onChange={(e) => set("whisper_language", e.target.value.trim() || null)} />
          </label>
        </div>
        <div className="muted small">
          On an 8 GB RTX 2060 Super: <b>small</b> (~1 GB VRAM) is the default; <b>medium</b> or <b>distil-large-v3</b> (English)
          are more accurate and still fit. The model downloads once into the ClipForge models folder.
        </div>
      </div>

      <div className="panel col">
        <h3>Rendering</h3>
        <div className="row">
          <label>Encoder
            <select value={form.encoder_preference} onChange={(e) => set("encoder_preference", e.target.value)}>
              <option value="auto">auto (verified hardware encoder, else CPU)</option>
              <option value="cpu">CPU (libx264)</option>
              <option value="h264_nvenc">NVIDIA NVENC</option>
              <option value="h264_qsv">Intel Quick Sync</option>
              <option value="h264_amf">AMD AMF</option>
            </select>
          </label>
          <label>Output size
            <select value={size} onChange={(e) => { const [w, h] = e.target.value.split("x").map(Number); setForm({ ...form, output_width: w, output_height: h }); }}>
              <option value="1080x1920">1080×1920</option><option value="720x1280">720×1280</option><option value="540x960">540×960 (drafts)</option>
            </select>
          </label>
          <label className="inline"><input type="checkbox" checked={form.deep_validate} onChange={(e) => set("deep_validate", e.target.checked)} /> full decode check after each render</label>
        </div>
        <label>Default export folder
          <input className="mono" value={form.default_export_dir} onChange={(e) => set("default_export_dir", e.target.value)} />
        </label>
      </div>
      <div className="row">
        <button className="primary" onClick={save}>Save settings</button>
        {msg && <span className="ok small">{msg}</span>}
        {err && <span className="err small">{err}</span>}
      </div>

      <div className="panel col">
        <h3>Integrations</h3>
        {connectMsg && <div className={connectMsg.ok ? "ok small" : "err small"}>{connectMsg.text}</div>}
        {Object.entries(platforms.data ?? {}).map(([platform, info]) => {
          const accs = (accounts.data ?? []).filter((a) => a.platform === platform);
          return (
            <div key={platform} className="col" style={{ gap: 6 }}>
              <div className="row between">
                <b>{PLATFORM_NAMES[platform] ?? platform}</b>
                {info.configured ? (
                  <button className="tiny" onClick={() => connect(platform)}>+ Connect account</button>
                ) : (
                  <span className="muted small">
                    Not set up — see README "Connecting {PLATFORM_NAMES[platform] ?? platform}".
                  </span>
                )}
              </div>
              {accs.length === 0 && <div className="muted small">No account connected.</div>}
              {accs.map((a) => (
                <div key={a.id} className="row between small" style={{ paddingLeft: 8 }}>
                  <div>
                    {a.display_name}{" "}
                    <span className={`chip ${a.status === "connected" ? "completed" : "failed"}`}>{a.status}</span>
                  </div>
                  <div className="row">
                    <label className="inline small">
                      <input type="checkbox" checked={a.auto_publish} disabled={a.status !== "connected"}
                             onChange={async (e) => {
                               await api.patchPublishAccount(a.id, e.target.checked);
                               accounts.refresh();
                             }} />
                      auto-publish
                    </label>
                    <ConfirmButton className="tiny ghost bad"
                                  onConfirm={async () => { await api.disconnectAccount(a.id); accounts.refresh(); }}>
                      Disconnect
                    </ConfirmButton>
                  </div>
                </div>
              ))}
            </div>
          );
        })}
        <div className="muted small">
          Auto-publish only applies per account, and only once you turn it on here — nothing posts on its own otherwise.
        </div>
      </div>

      <div className="panel col">
        <div className="row between">
          <h3>Diagnostics</h3>
          <button className="tiny" onClick={() => setDiagRefresh((n) => n + 1)}>Re-test hardware</button>
        </div>
        {diag.error && <div className="err">{diag.error}</div>}
        {d && (
          <div className="kv">
            <span>Version</span><span>{d.version} · Python {d.python} · {d.platform}</span>
            <span>Data folder</span><span className="mono">{d.data_dir}</span>
            <span>FFmpeg</span><span>{d.ffmpeg?.version ?? <span className="err">not found — {d.ffmpeg?.ffmpeg?.hint}</span>}</span>
            <span>H.264 encoders</span>
            <span className="col" style={{ gap: 2 }}>
              {(d.hardware?.encoders ?? []).map((e: any) => (
                <span key={e.name} className="small">
                  <span className={e.works ? "ok" : "muted"}>{e.works ? "✓" : "✕"}</span> <code>{e.name}</code>{" "}
                  <span className="muted">{e.detail}</span>
                </span>
              ))}
            </span>
            <span>CUDA (Whisper)</span><span>{d.hardware?.cuda_detail}</span>
            <span>Whisper models</span><span>{(d.transcription?.downloaded_models ?? []).join(", ") || "none downloaded yet"} <span className="muted mono small">{d.transcription?.models_dir}</span></span>
            <span>Packages</span><span className="small">{Object.entries(d.python_packages ?? {}).map(([k, v]) => `${k} ${v}`).join(" · ")}</span>
            <span>Disk free</span><span>{d.disk?.free_gb} GB of {d.disk?.total_gb} GB</span>
          </div>
        )}
      </div>
    </div>
  );
}

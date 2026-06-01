import { useMemo, useRef, useState } from "react";
import { Icon } from "./icons.jsx";
import { analyzeVideo, cacheVideo, parseVideo, prepareDownload } from "./api.js";
import { DEPENDENCIES, PLATFORMS, STEP_LABELS, cleanUrl, detectPlatform, extractShareUrl, progressForStatus } from "./platforms.js";

const BUSY_STATES = new Set(["parsing", "caching", "downloading", "analyzing"]);

function videoFileName(video) {
  const source = video.title || video.video_id || "video";
  const cleaned = source.replace(/[^\p{Letter}\p{Number}._ -]+/gu, "").trim();
  return `${(cleaned || video.video_id || "video").slice(0, 48)}.mp4`;
}

export default function App() {
  const [input, setInput] = useState("");
  const [platformId, setPlatformId] = useState("auto");
  const [status, setStatus] = useState("idle");
  const [message, setMessage] = useState("粘贴视频分享链接，或点一个平台示例开始。");
  const [video, setVideo] = useState(null);
  const [playback, setPlayback] = useState(null);
  const [download, setDownload] = useState(null);
  const [aiText, setAiText] = useState("");
  const [toast, setToast] = useState("");
  const toastTimer = useRef(null);

  const detectedPlatform = useMemo(() => detectPlatform(input, platformId), [input, platformId]);
  const progress = progressForStatus(status);
  const isBusy = BUSY_STATES.has(status);
  const canAct = Boolean(video?.video_url) && !isBusy;
  const cached = Boolean(playback?.play_url);
  const statusClass = status === "error" ? "is-error" : isBusy ? "is-busy" : status === "idle" ? "is-ready" : "is-complete";

  function notify(text) {
    setToast(text);
    if (toastTimer.current) window.clearTimeout(toastTimer.current);
    toastTimer.current = window.setTimeout(() => setToast(""), 2600);
  }

  function updateInput(nextInput) {
    setInput(nextInput);
    if (status === "error" && nextInput.trim()) {
      setStatus("idle");
      setMessage("链接已更新，可以重新解析。");
    }
  }

  function handlePlatformClick(platform) {
    setPlatformId(platform.id);
    if (platform.sample) {
      setInput(platform.sample);
      setStatus("idle");
      setMessage(`${platform.name} 示例链接已填入，可直接解析。`);
    }
  }

  async function handleParse() {
    const rawText = input.trim();
    if (!rawText) {
      setStatus("error");
      setMessage("请输入视频分享文案或链接。");
      return;
    }
    const extractedUrl = extractShareUrl(rawText);
    if (!extractedUrl) {
      setStatus("error");
      setMessage("分享内容里需要包含 http:// 或 https:// 视频链接。");
      return;
    }

    const platform = detectPlatform(rawText, platformId);
    const normalized = cleanUrl(rawText, platform.id);
    setStatus("parsing");
    setMessage(`已从分享文案提取链接，正在调用 /api/parse，平台策略：${platform.name}。`);
    setVideo(null);
    setPlayback(null);
    setDownload(null);
    setAiText("");

    try {
      const data = await parseVideo(normalized);
      setVideo({ ...data, original_url: normalized, cleaned_input: normalized, source_text: rawText });
      setStatus("parsed");
      setMessage("解析完成。下一步先在线播放写入本地缓存，或直接准备下载文件。");
    } catch (error) {
      setStatus("error");
      setMessage(error.message);
    }
  }

  async function handlePlayback() {
    if (!video) return;
    setStatus("caching");
    setMessage(video.platform === "哔哩哔哩" ? "正在缓存视频轨和音频轨，并准备 ffmpeg 合并。" : "正在写入后端播放缓存。");
    try {
      const data = await cacheVideo(video);
      setPlayback(data);
      setStatus("cached");
      setMessage("视频已进入本地缓存，可以预览、下载或进行 AI 内容提取。");
    } catch (error) {
      setStatus("error");
      setMessage(error.message);
    }
  }

  async function handleDownload() {
    if (!video) return;
    setStatus("downloading");
    setMessage(video.platform === "哔哩哔哩" ? "正在合并音视频并准备浏览器下载地址。" : "正在按安全文件名准备下载文件。");
    try {
      const data = await prepareDownload(video);
      setDownload(data);
      setStatus("downloaded");
      setMessage("下载文件已准备完成。");
    } catch (error) {
      setStatus("error");
      setMessage(error.message);
    }
  }

  async function handleAnalyze() {
    if (!video) return;
    if (!cached) {
      setStatus("error");
      setMessage("AI 提取前需要先在线播放，让视频进入本地缓存。");
      return;
    }
    setStatus("analyzing");
    setMessage("正在使用 ffprobe 获取时长，并用 ffmpeg 等间隔抽帧。");
    try {
      const data = await analyzeVideo(video);
      setAiText(data.content || "");
      setStatus("analyzed");
      setMessage("AI 内容提取完成，结果可复制到剪辑说明或素材库。");
    } catch (error) {
      setStatus("error");
      setMessage(error.message);
    }
  }

  function handleCopy() {
    if (!video) return;
    const payload = [
      `标题：${video.title || "无标题"}`,
      `平台：${video.platform || "未知"}`,
      `清理后链接：${video.cleaned_input || ""}`,
      `视频直链：${video.video_url || ""}`,
      aiText ? `AI 摘要：${aiText}` : "AI 摘要：尚未提取"
    ].join("\n");

    navigator.clipboard
      ?.writeText(payload)
      .then(() => notify("结果已复制。"))
      .catch(() => notify("当前环境不允许写入剪贴板。"));
  }

  function handleReset() {
    setInput("");
    setPlatformId("auto");
    setStatus("idle");
    setMessage("已清空输入、解析结果、缓存状态、下载文件和 AI 内容。");
    setVideo(null);
    setPlayback(null);
    setDownload(null);
    setAiText("");
  }

  return (
    <main className="app-shell">
      <Topbar />
      <div className="layout">
        <section className="workbench" aria-label="视频解析工作台">
          <InputPanel
            input={input}
            setInput={updateInput}
            platformId={platformId}
            detectedPlatform={detectedPlatform}
            onPlatformClick={handlePlatformClick}
            onParse={handleParse}
            onReset={handleReset}
            isBusy={isBusy}
          />
          <StatePanel status={status} statusClass={statusClass} progress={progress} message={message} />
          <ResultPanel
            video={video}
            playback={playback}
            download={download}
            aiText={aiText}
            status={status}
            canAct={canAct}
            cached={cached}
            onPlayback={handlePlayback}
            onDownload={handleDownload}
            onAnalyze={handleAnalyze}
            onCopy={handleCopy}
          />
        </section>
        <aside className="rail" aria-label="运行依赖与任务轨道">
          <DependencyPanel />
          <TimelinePanel status={status} video={video} cached={cached} downloadReady={Boolean(download?.download_url)} />
        </aside>
      </div>
      {toast ? <Toast message={toast} onClose={() => setToast("")} /> : null}
    </main>
  );
}

function Topbar() {
  return (
    <header className="topbar">
      <div className="brand">
        <div className="brand-mark"><Icon name="link" /></div>
        <div className="brand-copy">
          <h1 className="brand-title">视频解析工作台</h1>
          <div className="brand-subtitle">解析、缓存播放、下载与 AI 内容提取</div>
        </div>
      </div>
      <div className="connection-pill">API · 127.0.0.1:5001</div>
    </header>
  );
}

function InputPanel({ input, setInput, platformId, detectedPlatform, onPlatformClick, onParse, onReset, isBusy }) {
  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <h2 className="section-title">新建解析</h2>
          <p className="section-note">粘贴完整分享文案或链接，自动识别平台；小红书和快手会保留解析所需参数。</p>
        </div>
        <span className="status-chip is-ready"><span className="status-dot" />{detectedPlatform.name}</span>
      </div>
      <div className="panel-inner input-stack">
        <label className="url-field">
          <span className="field-label">
            <span>视频分享内容</span>
            <span>{input.length} chars</span>
          </span>
          <div className="input-row">
            <textarea
              className="url-input"
              value={input}
              onChange={(event) => setInput(event.target.value)}
              placeholder="粘贴抖音、B站、小红书、快手或好看视频分享文案/链接…"
              spellCheck="false"
            />
            <div className="quick-platforms" aria-label="平台示例">
              {PLATFORMS.map((platform) => (
                <button
                  className={`platform-button ${platformId === platform.id ? "is-selected" : ""}`}
                  key={platform.id}
                  type="button"
                  onClick={() => onPlatformClick(platform)}
                >
                  <span className="platform-name">{platform.name}</span>
                  <span className="platform-hint">{platform.hint}</span>
                </button>
              ))}
            </div>
          </div>
        </label>
        <div className="action-grid">
          <button className="btn btn-primary" type="button" onClick={onParse} disabled={isBusy}>
            <Icon name="link" />解析视频
          </button>
          <button className="btn btn-ghost" type="button" onClick={onReset} disabled={isBusy}>
            <Icon name="reset" />清空
          </button>
        </div>
      </div>
    </section>
  );
}

function StatePanel({ status, statusClass, progress, message }) {
  return (
    <section className="state-bar" aria-live="polite">
      <div className="state-top">
        <span className={`status-chip ${statusClass}`}><span className="status-dot" />{STEP_LABELS[status]}</span>
        <span className="mini-chip">{progress}%</span>
      </div>
      <div className="progress-track" aria-hidden="true">
        <div className="progress-fill" style={{ "--progress": `${progress}%` }} />
      </div>
      <p className="state-message">{message}</p>
    </section>
  );
}

function ResultPanel({ video, playback, download, aiText, status, canAct, cached, onPlayback, onDownload, onAnalyze, onCopy }) {
  if (!video) {
    return (
      <section className="panel">
        <div className="panel-inner">
          <div className="empty-state">
            <h2 className="empty-title">解析结果会在这里展开</h2>
            <p className="empty-copy">结果区包含标题、平台、封面、直链状态、播放缓存、下载文件和 AI 内容摘要。没有真实返回值时保持短提示，不伪造播放数据。</p>
          </div>
        </div>
      </section>
    );
  }

  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <h2 className="section-title">解析结果</h2>
          <p className="section-note">先播放写入 cache/，再下载或调用 AI 抽帧分析。</p>
        </div>
        <button className="btn btn-secondary btn-compact" type="button" onClick={onCopy}>
          <Icon name="copy" />复制
        </button>
      </div>
      <div className="panel-inner result-card">
        <CoverPanel video={video} playback={playback} />
        <div className="input-stack">
          <div className="meta-grid">
            <MetaItem label="清理后链接" value={video.cleaned_input || video.original_url || ""} />
            <MetaItem label="视频直链" value={video.video_url || "后端未返回"} />
            <MetaItem label="缓存路径" value={playback?.file_name ? `static/videos/${playback.file_name}` : "cache/ 等待播放写入"} />
            <MetaItem label="当前阶段" value={STEP_LABELS[status]} />
          </div>
          <div className="operation-row">
            <button className="btn btn-secondary" type="button" onClick={onPlayback} disabled={!canAct}>
              <Icon name="play" />在线播放
            </button>
            <button className="btn btn-secondary" type="button" onClick={onDownload} disabled={!canAct}>
              <Icon name="download" />下载视频
            </button>
            <button className="btn btn-primary" type="button" onClick={onAnalyze} disabled={!canAct}>
              <Icon name="spark" />AI 提取
            </button>
          </div>
          {download?.download_url ? <DownloadFile video={video} download={download} /> : null}
          <AiPanel aiText={aiText} cached={cached} />
        </div>
      </div>
    </section>
  );
}

function CoverPanel({ video, playback }) {
  const cover = video.cover_url;
  return (
    <div className="cover-frame">
      {cover ? <img className="cover-image" src={cover} alt={`${video.title || "视频"}封面`} /> : <div className="cover-grid" />}
      <div className="cover-content">
        {playback?.play_url ? (
          <video className="video-preview" controls src={playback.play_url} poster={cover || undefined} />
        ) : (
          <div className="play-disc"><Icon name="play" /></div>
        )}
        <div className="cover-copy">
          <span className="platform-tag">{video.platform || "未知平台"} · {video.video_id || "video_id"}</span>
          <h3 className="video-title">{video.title || "无标题视频"}</h3>
        </div>
      </div>
    </div>
  );
}

function MetaItem({ label, value }) {
  return (
    <div className="meta-item">
      <div className="meta-label">{label}</div>
      <div className="meta-value">{value}</div>
    </div>
  );
}

function DownloadFile({ video, download }) {
  return (
    <div className="download-file">
      <div className="download-name">{download.file_name || videoFileName(video)}</div>
      <a className="mini-chip mini-link" href={download.download_url} download={download.file_name || videoFileName(video)}>
        下载
      </a>
    </div>
  );
}

function AiPanel({ aiText, cached }) {
  return (
    <div className="ai-panel">
      <div className="ai-header">
        <h3 className="ai-title">AI 内容提取</h3>
        <span className="mini-chip">{cached ? "可执行" : "需先播放"}</span>
      </div>
      <p className="ai-content">
        {aiText || "播放缓存完成后，这里展示 ffprobe 时长、ffmpeg 抽帧与模型返回的中文内容描述。"}
      </p>
    </div>
  );
}

function DependencyPanel() {
  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <h2 className="section-title">运行依赖</h2>
          <p className="section-note">把原 Gradio 单页的隐性依赖前置成可检查状态。</p>
        </div>
      </div>
      <div className="panel-inner dependency-list">
        {DEPENDENCIES.map((item) => (
          <div className="dependency-item" key={item.name}>
            <div className="dependency-copy">
              <p className="dependency-name">{item.name}</p>
              <p className="dependency-desc">{item.desc}</p>
            </div>
            <span className={`dependency-state ${item.state === "warn" ? "is-warn" : ""}`}>{item.label}</span>
          </div>
        ))}
      </div>
    </section>
  );
}

function TimelinePanel({ status, video, cached, downloadReady }) {
  const steps = [
    { id: "parse", title: "解析元数据", note: video ? "平台、标题、封面与直链状态已回填。" : "调用 /api/parse 前等待输入。", done: Boolean(video) },
    { id: "cache", title: "播放写入缓存", note: cached ? "cache/ 路径已准备，可复用播放文件。" : "B站会在这里显示音视频合并进度。", done: cached },
    { id: "download", title: "下载文件", note: downloadReady ? "downloads/ 文件地址已准备。" : "按标题生成安全文件名。", done: downloadReady },
    { id: "ai", title: "AI 内容提取", note: status === "analyzed" ? "中文摘要已生成，可复制。" : "需要缓存后才能抽帧分析。", done: status === "analyzed" }
  ];

  return (
    <section className="panel">
      <div className="panel-header">
        <div>
          <h2 className="section-title">任务轨道</h2>
          <p className="section-note">主路径保持线性，减少原页面按钮并列造成的下一步不明确。</p>
        </div>
      </div>
      <div className="panel-inner timeline">
        {steps.map((step, index) => (
          <div className={`event ${step.done ? "is-done" : ""}`} key={step.id}>
            <div className="event-dot">{index + 1}</div>
            <div className="event-copy">
              <p className="event-title">{step.title}</p>
              <p className="event-note">{step.note}</p>
            </div>
          </div>
        ))}
      </div>
    </section>
  );
}

function Toast({ message, onClose }) {
  return (
    <div className="toast" role="status">
      <p>{message}</p>
      <button type="button" aria-label="关闭提示" onClick={onClose}>×</button>
    </div>
  );
}

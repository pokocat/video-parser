export const PLATFORMS = [
  { id: "auto", name: "自动检测", hint: "auto", sample: "" },
  {
    id: "douyin",
    name: "抖音",
    hint: "douyin",
    sample: "https://www.douyin.com/note/7580598241298069157"
  },
  {
    id: "bilibili",
    name: "哔哩哔哩",
    hint: "bilibili",
    sample: "https://www.bilibili.com/video/BV1TaqYBcEJc"
  },
  {
    id: "xiaohongshu",
    name: "小红书",
    hint: "xhs",
    sample: "https://www.xiaohongshu.com/explore/68ab2dd1000000001c0045d0?xsec_source=app_share&type=video&xsec_token=CBLONm9tab3493BJUXCtvU8ScDzfYqa3cGwJstW8eSF3Y="
  },
  {
    id: "kuaishou",
    name: "快手",
    hint: "kuaishou",
    sample: "https://www.kuaishou.com/short-video/3x8zha3ipq6bg8q"
  },
  {
    id: "haokan",
    name: "好看视频",
    hint: "haokan",
    sample: "https://haokan.baidu.com/v?vid=13766973483433940333&tab=recommend"
  }
];

export const STEP_LABELS = {
  idle: "等待输入",
  parsing: "解析中",
  parsed: "解析完成",
  caching: "缓存播放中",
  cached: "已进入缓存",
  downloading: "下载中",
  downloaded: "下载就绪",
  analyzing: "AI 提取中",
  analyzed: "AI 已完成",
  error: "需要处理"
};

export const DEPENDENCIES = [
  { name: "FastAPI 后端", desc: "默认调用 5001 端口的解析接口", state: "ready", label: "5001" },
  { name: "ffmpeg / ffprobe", desc: "播放缓存、B站合并、AI 抽帧依赖", state: "warn", label: "本机" },
  { name: "QWEN_API_KEY", desc: "AI 视频内容提取使用 OpenAI 兼容接口", state: "warn", label: "ENV" }
];

export function detectPlatform(value, selected) {
  const text = value.toLowerCase();
  if (selected && selected !== "auto") {
    return PLATFORMS.find((item) => item.id === selected) || PLATFORMS[0];
  }
  if (text.includes("douyin") || text.includes("iesdouyin")) {
    return PLATFORMS.find((item) => item.id === "douyin");
  }
  if (text.includes("bilibili") || text.includes("b23.tv") || text.includes("bv")) {
    return PLATFORMS.find((item) => item.id === "bilibili");
  }
  if (text.includes("xiaohongshu") || text.includes("xhslink")) {
    return PLATFORMS.find((item) => item.id === "xiaohongshu");
  }
  if (text.includes("kuaishou")) {
    return PLATFORMS.find((item) => item.id === "kuaishou");
  }
  if (text.includes("haokan.baidu.com") || text.includes("haokan.hao123.com")) {
    return PLATFORMS.find((item) => item.id === "haokan");
  }
  return PLATFORMS[0];
}

export function cleanUrl(value, platformId) {
  try {
    const url = new URL(value.trim());
    if (platformId === "xiaohongshu") {
      Array.from(url.searchParams.keys()).forEach((key) => {
        if (!["xsec_token", "xsec_source"].includes(key)) {
          url.searchParams.delete(key);
        }
      });
    }
    if (platformId === "kuaishou") {
      Array.from(url.searchParams.keys()).forEach((key) => {
        if (!["fid", "photoId"].includes(key)) {
          url.searchParams.delete(key);
        }
      });
    }
    return url.toString();
  } catch {
    return value.trim();
  }
}

export function progressForStatus(status) {
  return {
    idle: 0,
    parsing: 34,
    parsed: 48,
    caching: 62,
    cached: 72,
    downloading: 82,
    downloaded: 88,
    analyzing: 94,
    analyzed: 100,
    error: 18
  }[status] ?? 0;
}

function timestampToLetters(timestamp) {
  const letters = "abcdefghijklmnopqrstuvwxyz";
  return String(timestamp)
    .split("")
    .map((char) => (/\d/.test(char) ? letters[Number(char)] : "a"))
    .join("");
}

function vigenereEncrypt(text, timestamp) {
  const key = timestampToLetters(timestamp).toLowerCase();
  let keyIndex = 0;
  return text
    .split("")
    .map((char) => {
      if (!/[a-z]/i.test(char)) return char;
      const base = char >= "A" && char <= "Z" ? 65 : 97;
      const keyShift = key.charCodeAt(keyIndex % key.length) - 97;
      keyIndex += 1;
      return String.fromCharCode(((char.charCodeAt(0) - base + keyShift) % 26) + base);
    })
    .join("");
}

function randomText(length = 32) {
  const chars = "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ";
  let value = "";
  crypto.getRandomValues(new Uint8Array(length)).forEach((byte) => {
    value += chars[byte % chars.length];
  });
  return value;
}

function authHeaders() {
  const timestamp = String(Date.now());
  const original = randomText();
  return {
    "Content-Type": "application/json",
    "X-Timestamp": timestamp,
    "X-GCLT-Text": original,
    "X-EGCT-Text": vigenereEncrypt(original, timestamp),
    "WX-OPEN-ID": "react-workbench"
  };
}

async function postJson(path, payload) {
  const response = await fetch(path, {
    method: "POST",
    headers: authHeaders(),
    body: JSON.stringify(payload)
  });
  const result = await response.json().catch(() => null);
  if (!response.ok || !result?.succ) {
    throw new Error(result?.retdesc || `请求失败 (${response.status})`);
  }
  return result.data || {};
}

export async function parseVideo(text) {
  return postJson("/api/parse", { text });
}

export async function cacheVideo(video) {
  return postJson("/api/frontend/cache", {
    video_id: video.video_id || "video",
    platform: video.platform,
    title: video.title,
    video_url: video.video_url,
    audio_url: video.audio_url,
    original_url: video.original_url
  });
}

export async function prepareDownload(video) {
  return postJson("/api/frontend/download", {
    video_id: video.video_id || "video",
    platform: video.platform,
    title: video.title,
    video_url: video.video_url,
    audio_url: video.audio_url,
    original_url: video.original_url
  });
}

export async function analyzeVideo(video) {
  return postJson("/api/frontend/analyze", {
    video_id: video.video_id || "video",
    platform: video.platform,
    title: video.title,
    video_url: video.video_url,
    audio_url: video.audio_url,
    original_url: video.original_url
  });
}

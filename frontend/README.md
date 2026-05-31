# 视频解析工作台前端

这是基于附件 Open Design 交付稿新增的 React/Vite 前端界面。它保留现有 Gradio 入口，不替换 `app.py`。

## 本地运行

```bash
cd frontend
npm install
npm run dev
```

访问：`http://localhost:5173`

前端默认通过 Vite proxy 调用本机 FastAPI：`http://127.0.0.1:5001`。

## 对接接口

- `POST /api/parse`：解析链接，获取标题、封面、视频直链、B站音频直链。
- `POST /api/frontend/cache`：后端缓存可播放视频，B站会合并音视频。
- `POST /api/frontend/download`：准备浏览器可下载的静态文件地址。
- `POST /api/frontend/analyze`：使用缓存视频抽帧并调用 Qwen3-VL 生成中文内容描述。

AI 内容提取需要后端配置 `QWEN_API_KEY`。

import { useEffect, useState } from "react";
import { api, type WeworkSendModes } from "../api";

type Props = {
  onToast: (msg: string) => void;
};

type SendResult = {
  plan: string;
  result: Record<string, unknown>;
};

export function WeworkSendPage({ onToast }: Props) {
  const [chatId, setChatId] = useState("");
  const [toExternalUserid, setToExternalUserid] = useState("");
  const [content, setContent] = useState("");
  const [modes, setModes] = useState<WeworkSendModes | null>(null);
  const [modesError, setModesError] = useState<string | null>(null);
  const [sending, setSending] = useState(false);
  const [lastResult, setLastResult] = useState<SendResult | null>(null);

  useEffect(() => {
    let alive = true;
    api.wework
      .sendModes()
      .then((d) => {
        if (alive) setModes(d);
      })
      .catch((e) => {
        if (alive) setModesError((e as Error).message);
      });
    return () => {
      alive = false;
    };
  }, []);

  async function onSend() {
    if (!chatId.trim()) {
      onToast("请输入 chat_id（外部群 ID 形如 wrXXXX）");
      return;
    }
    if (!content.trim()) {
      onToast("请输入消息内容");
      return;
    }
    if (new TextEncoder().encode(content).length > 2000) {
      onToast("消息内容超过 2000 字节");
      return;
    }
    setSending(true);
    setLastResult(null);
    try {
      const res = await api.wework.send(
        chatId.trim(),
        content,
        toExternalUserid.trim() || undefined
      );
      setLastResult(res);
      onToast("发送成功");
    } catch (e) {
      onToast((e as Error).message || "发送失败");
    } finally {
      setSending(false);
    }
  }

  return (
    <div className="register-page">
      <div className="reg-grid">
        <section className="reg-card">
          <h3 className="reg-section-title">手动发送到外部群</h3>
          <div className="reg-form" style={{ gridTemplateColumns: "1fr" }}>
            <label className="reg-field">
              <span>
                chat_id<em>*</em>
              </span>
              <input
                type="text"
                value={chatId}
                onChange={(e) => setChatId(e.target.value)}
                placeholder="wrXXXXXXXXXXXXXX"
                disabled={sending}
              />
              <small style={{ color: "#888", fontSize: "0.78rem" }}>
                外部客户群 ID（企微回调的 ChatId 字段）；客服私聊会话 ID（kf:wkXXX:wmYYY）也可
              </small>
            </label>
            <label className="reg-field">
              <span>指定外部联系人（可选）</span>
              <input
                type="text"
                value={toExternalUserid}
                onChange={(e) => setToExternalUserid(e.target.value)}
                placeholder="wmXXXX（仅 kf 模式私聊该客户）"
                disabled={sending}
              />
            </label>
            <label className="reg-field">
              <span>
                消息内容<em>*</em>
              </span>
              <textarea
                className="reg-paste-area"
                style={{ minHeight: 140 }}
                value={content}
                onChange={(e) => setContent(e.target.value)}
                placeholder="要发送到群里的文本消息（≤2000 字节）"
                disabled={sending}
              />
              <small style={{ color: "#888", fontSize: "0.78rem" }}>
                当前字节：{new TextEncoder().encode(content).length} / 2000
              </small>
            </label>
          </div>
          <div className="reg-actions">
            <button
              type="button"
              className="btn btn-primary"
              onClick={onSend}
              disabled={sending}
            >
              {sending ? "发送中…" : "发送到外部群"}
            </button>
            <button
              type="button"
              className="btn-ghost"
              onClick={() => {
                setContent("");
                setLastResult(null);
              }}
              disabled={sending}
            >
              清空
            </button>
          </div>
        </section>

        <section className="reg-card">
          <h3 className="reg-section-title">当前发送配置</h3>
          {modesError ? (
            <div className="error-box">{modesError}</div>
          ) : !modes ? (
            <div className="state-box">加载中…</div>
          ) : (
            <div style={{ fontSize: "0.85rem", lineHeight: 1.9 }}>
              <div>
                发送模式：
                <strong style={{ marginLeft: 6 }}>
                  {modes.send_mode}
                </strong>
                <span style={{ color: "#888", marginLeft: 8, fontSize: "0.78rem" }}>
                  {modes.send_mode === "kf" && "（客服私聊，即时，无需群主确认）"}
                  {modes.send_mode === "mass" && "（企业群发，需群主在企微确认）"}
                  {modes.send_mode === "webhook" && "（群机器人 webhook，即时入群）"}
                  {modes.send_mode === "appchat" && "（内部群 API，外部群不可用）"}
                </span>
              </div>
              <div>
                企微已配置：
                <strong style={{ marginLeft: 6 }}>
                  {modes.configured ? "✓" : "✗"}
                </strong>
              </div>
              <div>
                客服已配置：
                <strong style={{ marginLeft: 6 }}>
                  {modes.kf_configured ? "✓" : "✗"}
                </strong>
              </div>
              <div>
                双通道：
                <strong style={{ marginLeft: 6 }}>{modes.channel}</strong>
              </div>
              <div>
                群 webhook：
                <strong style={{ marginLeft: 6 }}>
                  {modes.webhook_url_set ? "已配置" : "未配置"}
                </strong>
              </div>
              <div>
                默认群主 userid：
                <strong style={{ marginLeft: 6 }}>
                  {modes.default_owner_set ? "已配置" : "未配置"}
                </strong>
              </div>
            </div>
          )}

          <h3 className="reg-section-title" style={{ marginTop: 18 }}>
            发送结果
          </h3>
          {!lastResult ? (
            <div className="empty-box">尚未发送</div>
          ) : (
            <div style={{ fontSize: "0.82rem" }}>
              <div style={{ marginBottom: 8 }}>
                <strong>发送计划：</strong>
                <div style={{ color: "#666", marginTop: 4 }}>{lastResult.plan}</div>
              </div>
              <div>
                <strong>企微返回：</strong>
                <pre
                  style={{
                    background: "#f7f7f8",
                    padding: 8,
                    borderRadius: 4,
                    marginTop: 4,
                    fontSize: "0.75rem",
                    maxHeight: 200,
                    overflow: "auto",
                  }}
                >
                  {JSON.stringify(lastResult.result, null, 2)}
                </pre>
              </div>
            </div>
          )}
        </section>
      </div>
    </div>
  );
}

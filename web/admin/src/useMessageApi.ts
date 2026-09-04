import { createContext, useContext } from "react";
import type { MessageInstance } from "antd/es/message/interface";

export const MessageCtx = createContext<MessageInstance | null>(null);

export function useMessageApi() {
  const api = useContext(MessageCtx);
  if (!api) {
    throw new Error("useMessageApi outside MessageApiProvider");
  }
  return api;
}

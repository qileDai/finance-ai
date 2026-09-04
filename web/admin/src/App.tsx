import { useCallback, useState, type ReactNode } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { App as AntdApp, ConfigProvider, message } from "antd";
import zhCN from "antd/locale/zh_CN";
import { AuthProvider, RequireAuth } from "./auth";
import { Layout } from "./components/Layout";
import { JobsPage } from "./pages/JobsPage";
import { JobDetailPage } from "./pages/JobDetailPage";
import { LoginPage } from "./pages/LoginPage";
import { OverviewPage } from "./pages/OverviewPage";
import { QualityPage } from "./pages/QualityPage";
import { RegisterPage } from "./pages/RegisterPage";
import { EmailConfigPage } from "./pages/EmailConfigPage";
import { DefaultOfficePage } from "./pages/DefaultOfficePage";
import { IdExtractPage } from "./pages/IdExtractPage";
import { SessionDetailPage, SessionsPage } from "./pages/SessionsPage";
import { WeworkSendPage } from "./pages/WeworkSendPage";
import { MessageCtx } from "./useMessageApi";

function MessageApiProvider({ children }: { children: ReactNode }) {
  const [api, holder] = message.useMessage({
    duration: 3,
    maxCount: 3,
    getContainer: () => document.body,
  });
  return (
    <MessageCtx.Provider value={api}>
      {holder}
      {children}
    </MessageCtx.Provider>
  );
}

export default function App() {
  const [refreshKey, setRefreshKey] = useState(0);

  const onRefresh = useCallback(() => setRefreshKey((k) => k + 1), []);

  return (
    <ConfigProvider
      locale={zhCN}
      theme={{
        token: {
          colorPrimary: "#1677ff",
          borderRadius: 6,
        },
      }}
    >
      <AntdApp style={{ height: "100%" }}>
        <MessageApiProvider>
          <BrowserRouter basename="/admin">
            <AuthProvider>
              <Routes>
                <Route path="login" element={<LoginPage />} />
                <Route
                  element={
                    <RequireAuth>
                      <Layout />
                    </RequireAuth>
                  }
                >
                  <Route index element={<OverviewPage refreshKey={refreshKey} />} />
                  <Route path="sessions" element={<SessionsPage refreshKey={refreshKey} />} />
                  <Route
                    path="sessions/:roomid"
                    element={<SessionDetailPage refreshKey={refreshKey} />}
                  />
                  <Route
                    path="jobs"
                    element={
                      <JobsPage refreshKey={refreshKey} onRefresh={onRefresh} />
                    }
                  />
                  <Route
                    path="jobs/:id"
                    element={
                      <JobDetailPage refreshKey={refreshKey} onRefresh={onRefresh} />
                    }
                  />
                  <Route path="register" element={<RegisterPage />} />
                  <Route path="email-config" element={<EmailConfigPage />} />
                  <Route path="default-office" element={<DefaultOfficePage />} />
                  <Route path="id-extract" element={<IdExtractPage />} />
                  <Route path="wework-send" element={<WeworkSendPage />} />
                  <Route path="quality" element={<QualityPage refreshKey={refreshKey} />} />
                  <Route path="groups" element={<Navigate to="/sessions" replace />} />
                  <Route path="*" element={<Navigate to="/" replace />} />
                </Route>
              </Routes>
            </AuthProvider>
          </BrowserRouter>
        </MessageApiProvider>
      </AntdApp>
    </ConfigProvider>
  );
}

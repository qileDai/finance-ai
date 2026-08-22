import { useCallback, useEffect, useState } from "react";
import { BrowserRouter, Navigate, Route, Routes } from "react-router-dom";
import { ConfigProvider } from "antd";
import zhCN from "antd/locale/zh_CN";
import { AuthProvider, RequireAuth } from "./auth";
import { Layout } from "./components/Layout";
import { JobsPage } from "./pages/JobsPage";
import { JobDetailPage } from "./pages/JobDetailPage";
import { LoginPage } from "./pages/LoginPage";
import { OverviewPage } from "./pages/OverviewPage";
import { QualityPage } from "./pages/QualityPage";
import { RegisterPage } from "./pages/RegisterPage";
import { IdExtractPage } from "./pages/IdExtractPage";
import { SessionDetailPage, SessionsPage } from "./pages/SessionsPage";
import { WeworkSendPage } from "./pages/WeworkSendPage";

export default function App() {
  const [refreshKey, setRefreshKey] = useState(0);
  const [toast, setToast] = useState("");

  const onRefresh = useCallback(() => setRefreshKey((k) => k + 1), []);

  const onToast = useCallback((msg: string) => {
    setToast(msg);
  }, []);

  useEffect(() => {
    if (!toast) return;
    const t = window.setTimeout(() => setToast(""), 3200);
    return () => window.clearTimeout(t);
  }, [toast]);

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
      <BrowserRouter basename="/admin">
        <AuthProvider>
          <Routes>
          <Route path="login" element={<LoginPage />} />
          <Route
            element={
              <RequireAuth>
                <Layout onRefresh={onRefresh} toast={toast} />
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
                <JobsPage
                  refreshKey={refreshKey}
                  onToast={onToast}
                  onRefresh={onRefresh}
                />
              }
            />
            <Route
              path="jobs/:id"
              element={
                <JobDetailPage
                  refreshKey={refreshKey}
                  onToast={onToast}
                  onRefresh={onRefresh}
                />
              }
            />
            <Route
              path="register"
              element={<RegisterPage onToast={onToast} />}
            />
            <Route
              path="id-extract"
              element={<IdExtractPage onToast={onToast} />}
            />
            <Route
              path="wework-send"
              element={<WeworkSendPage onToast={onToast} />}
            />
            <Route path="quality" element={<QualityPage refreshKey={refreshKey} />} />
            <Route path="groups" element={<Navigate to="/sessions" replace />} />
            <Route path="*" element={<Navigate to="/" replace />} />
          </Route>
        </Routes>
        </AuthProvider>
      </BrowserRouter>
    </ConfigProvider>
  );
}

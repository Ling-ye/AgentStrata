import React from "react";
import ReactDOM from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import App from "./App";
import LauncherSession from "./shared/LauncherSession";
import { prepareLauncherSession } from "./shared/launcherSession";
import "@arco-design/web-react/dist/css/arco.css";
import "react-resizable/css/styles.css";
import "./styles.css";

const launcherLogin = prepareLauncherSession(window.location, window.history);

const queryClient = new QueryClient({
  defaultOptions: {
    queries: {
      staleTime: 5_000,
      retry: 1,
      refetchOnWindowFocus: false,
    },
  },
});

ReactDOM.createRoot(document.getElementById("root")!).render(
  <React.StrictMode>
    <QueryClientProvider client={queryClient}>
      <LauncherSession login={launcherLogin}><App /></LauncherSession>
    </QueryClientProvider>
  </React.StrictMode>,
);

import { createRoot } from "react-dom/client";
import { readLoginCode } from "./admin";
import { App } from "./App";
import "./styles.css";

const root = document.getElementById("root");
if (!root) throw new Error("Missing settings application root");
createRoot(root).render(<App loginCode={readLoginCode()} />);

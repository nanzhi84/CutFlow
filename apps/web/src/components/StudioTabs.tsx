import { Bot, Edit3, Film, RadioTower, Rows3, SlidersHorizontal } from "lucide-react";
import { NavLink } from "react-router-dom";
import { routes } from "../routes";

export function StudioTabs({ caseId }: { caseId: string }) {
  const tabs = [
    { to: routes.caseStudio(caseId), label: "创作", icon: Edit3, end: true },
    { to: routes.caseProfile(caseId), label: "案例画像", icon: SlidersHorizontal },
    { to: routes.caseAgent(caseId), label: "评分卡", icon: Bot },
    { to: routes.caseBatch(caseId), label: "批量", icon: Rows3 },
    { to: routes.caseOutputs(caseId), label: "成片", icon: Film },
    { to: routes.casePublish(caseId), label: "发布", icon: RadioTower },
  ];
  return (
    <nav className="tabs" aria-label="Case 工作台">
      {tabs.map((tab) => {
        const Icon = tab.icon;
        return (
          <NavLink className="tabLink" to={tab.to} end={tab.end} key={tab.to}>
            <Icon size={15} />
            <span>{tab.label}</span>
          </NavLink>
        );
      })}
    </nav>
  );
}

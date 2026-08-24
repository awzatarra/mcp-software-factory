import { Link, useLocation } from "react-router-dom";

const items = [
  ["Alertas", "/alerts"], ["Reglas", "/alerts?view=rules"],
  ["Canales", "/notifications/channels"], ["Políticas", "/notifications/policies"],
  ["Entregas", "/notifications/deliveries"],
];

export function NotificationNavigation() {
  const location = useLocation();
  return <nav className="alerts-tabs" aria-label="Gestión de alertas y notificaciones">{items.map(([label, path]) => <Link aria-current={`${location.pathname}${location.search}` === path || (path === "/alerts" && location.pathname === "/alerts" && !location.search) ? "page" : undefined} key={path} to={path}>{label}</Link>)}</nav>;
}

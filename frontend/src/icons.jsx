export function Icon({ name }) {
  const common = {
    className: "icon",
    viewBox: "0 0 24 24",
    fill: "none",
    stroke: "currentColor",
    strokeWidth: "2",
    strokeLinecap: "round",
    strokeLinejoin: "round",
    "aria-hidden": "true"
  };

  if (name === "play") {
    return <svg {...common}><path d="M8 5v14l11-7-11-7Z" fill="currentColor" stroke="none" /></svg>;
  }
  if (name === "download") {
    return <svg {...common}><path d="M12 3v12" /><path d="m7 10 5 5 5-5" /><path d="M5 21h14" /></svg>;
  }
  if (name === "spark") {
    return <svg {...common}><path d="M13 2 9.5 9.5 2 13l7.5 3.5L13 22l3.5-5.5L22 13l-5.5-3.5L13 2Z" /></svg>;
  }
  if (name === "copy") {
    return <svg {...common}><rect x="8" y="8" width="12" height="12" rx="2" /><path d="M4 16V6a2 2 0 0 1 2-2h10" /></svg>;
  }
  if (name === "reset") {
    return <svg {...common}><path d="M4 12a8 8 0 1 0 2.34-5.66" /><path d="M4 4v6h6" /></svg>;
  }
  if (name === "link") {
    return <svg {...common}><path d="M10 13a5 5 0 0 0 7.07 0l2.12-2.12a5 5 0 0 0-7.07-7.07L11 4.93" /><path d="M14 11a5 5 0 0 0-7.07 0L4.81 13.12a5 5 0 0 0 7.07 7.07L13 19.07" /></svg>;
  }
  return <svg {...common}><path d="M4 12h16" /><path d="M12 4v16" /></svg>;
}

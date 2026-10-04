import { useState } from "react";
import { afterEach, beforeEach, describe, expect, it } from "vitest";
import { cleanup, fireEvent, render, screen } from "@testing-library/react";
import { MessageReader } from "./main";
function Harness() {
  const [open,setOpen]=useState(false);
  return <><button onClick={()=>setOpen(true)}>Expand message</button>{open && <MessageReader onClose={()=>setOpen(false)}><p>A response with a source.</p><a href="https://example.com">Original source</a><table><tbody><tr><td>2026-09-30</td></tr></tbody></table></MessageReader>}</>;
}
const originalShowModal = Object.getOwnPropertyDescriptor(HTMLDialogElement.prototype, "showModal");
beforeEach(()=>{
  Object.defineProperty(HTMLDialogElement.prototype,"showModal", {configurable:true, value:function(this: HTMLDialogElement){ this.open=true; }});
});
afterEach(()=>{
  cleanup();
  if (originalShowModal) Object.defineProperty(HTMLDialogElement.prototype,"showModal",originalShowModal);
  else Reflect.deleteProperty(HTMLDialogElement.prototype,"showModal");
});
describe("wide message reader",()=>{
  it("shows the full message with an accessible scroll region and restores focus",()=>{
    render(<Harness />);
    const opener=screen.getByRole("button",{name:"Expand message"}); opener.focus(); fireEvent.click(opener);
    expect(screen.getByRole("dialog",{name:"Agent response"})).not.toBeNull();
    expect(screen.getByRole("region",{name:/Full message/}).tabIndex).toBe(0);
    expect(screen.getByRole("link",{name:"Original source"}).getAttribute("href")).toBe("https://example.com");
    expect(screen.getByText("2026-09-30")).not.toBeNull();
    fireEvent.click(screen.getByRole("button",{name:"Close message"}));
    expect(screen.queryByRole("dialog")).toBeNull(); expect(document.activeElement).toBe(opener);
  });
  it("closes on Escape's native cancel event",()=>{
    render(<Harness />); fireEvent.click(screen.getByRole("button",{name:"Expand message"}));
    fireEvent(screen.getByRole("dialog"),new Event("cancel",{bubbles:false}));
    expect(screen.queryByRole("dialog")).toBeNull();
  });
});

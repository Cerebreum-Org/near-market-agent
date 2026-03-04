import { describe, it, expect } from "vitest";
import { hello, VERSION } from "../src/index.js";

describe("package", () => {
  it("should export a version", () => {
    expect(VERSION).toBeDefined();
    expect(typeof VERSION).toBe("string");
  });

  it("should greet by name", () => {
    expect(hello("World")).toBe("Hello, World!");
  });
});

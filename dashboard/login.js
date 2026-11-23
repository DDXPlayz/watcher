"use strict";
document.getElementById("login").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.getElementById("error");
  error.textContent = "";
  try {
    const response = await fetch("/api/login", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ password: new FormData(event.target).get("password") }),
    });
    if (response.ok) {
      location.replace("/");
      return;
    }
    error.textContent = (await response.json()).detail || "Sign in failed.";
  } catch {
    error.textContent = "The watcher did not answer. Check that it is running.";
  }
});

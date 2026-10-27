/**
 * APAARSetu — Landing Page & 4-Step Governance Flow Controller
 * Vanilla ES6 JavaScript without external dependencies.
 */
(() => {
  "use strict";

  let activeStep = 1;
  const totalSteps = 4;

  document.addEventListener("DOMContentLoaded", () => {
    initStepper();
    initDemoLogin();
  });

  function initStepper() {
    const circles = document.querySelectorAll(".tour-step-circle");

    circles.forEach(circle => {
      circle.addEventListener("click", () => {
        const step = parseInt(circle.dataset.step, 10);
        if (!isNaN(step)) {
          setStep(step);
        }
      });
    });

    setStep(1);
  }

  function setStep(step) {
    if (step < 1) step = 1;
    if (step > totalSteps) step = totalSteps;
    activeStep = step;

    // Update circles styling
    const circles = document.querySelectorAll(".tour-step-circle");
    circles.forEach(circle => {
      const s = parseInt(circle.dataset.step, 10);
      circle.setAttribute("aria-pressed", String(s === activeStep));
      if (s === activeStep) {
        circle.style.background = "var(--navy)";
        circle.style.color = "#ffffff";
        circle.style.border = "3px solid #ffffff";
        circle.style.boxShadow = "0 2px 6px rgba(20,49,92,0.25)";
        circle.style.transform = "scale(1.08)";
      } else {
        circle.style.background = "var(--surface)";
        circle.style.color = "var(--navy)";
        circle.style.border = "2px solid var(--border)";
        circle.style.boxShadow = "none";
        circle.style.transform = "scale(1)";
      }
    });

    // Toggle corresponding step cards
    for (let i = 1; i <= totalSteps; i++) {
      const card = document.getElementById(`step-card-${i}`);
      if (card) {
        card.style.display = i === activeStep ? "block" : "none";
      }
    }
  }

  function initDemoLogin() {
    const buttons = document.querySelectorAll(".demo-btn");
    buttons.forEach(btn => {
      btn.addEventListener("click", async () => {
        const username = btn.dataset.user;
        const password = btn.dataset.pwd;
        if (!username || !password) return;

        const original = btn.textContent;
        btn.disabled = true;
        btn.textContent = "Logging in...";

        try {
          const csrf = document.querySelector('meta[name="csrf-token"]')?.getAttribute("content") || "";
          const res = await fetch("/api/auth/login", {
            method: "POST",
            headers: {
              "Content-Type": "application/json",
              "X-CSRFToken": csrf
            },
            body: JSON.stringify({ username, password })
          });
          const data = await res.json();
          if (data.ok) {
            window.location.href = data.data?.redirect_url || "/dashboard";
          } else {
            alert(data.error?.message || "Sign in failed");
            btn.disabled = false;
            btn.textContent = original;
          }
        } catch (e) {
          alert("Network error. Please try again.");
          btn.disabled = false;
          btn.textContent = original;
        }
      });
    });
  }
})();

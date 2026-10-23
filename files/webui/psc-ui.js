// PSC Robotics additions to kit-ui:
//  - camera calibration in a dialog on the main page (toolbar button, or /#calibrate)
//  - a banner when a connected camera isn't calibrated
//  - the latest image from every robot camera in kit-ui's camera view
//  - pairing Bluetooth gamepads, and testing connected gamepads (toolbar button, or /#gamepads)
(() => {
	"use strict";

	const BOARD = { rows: 4, columns: 6, marker_size_mm: 40, minimum_markers: 6 };
	const MINIMUM_VIEWS = 10;
	const GOOD_VIEWS = 15;
	const MAX_ERROR_PX = 2;
	const POSES = [
		["crop_free", "Hold the board straight on, in the middle of the picture", "Keep it still while you capture."],
		["north_west", "Move it to the top-left of the picture", "The coverage map shows where you've been."],
		["north_east", "Move it to the top-right"],
		["south_west", "Move it to the bottom-left"],
		["south_east", "Move it to the bottom-right"],
		["zoom_in", "Bring it close, so it nearly fills the picture", "All tags still need to be sharp and inside the picture."],
		["zoom_out", "Move it further away, to about a third of the picture"],
		["rotate_left", "Turn it to the left by about 30°"],
		["rotate_right", "Turn it to the right by about 30°"],
		["flip", "Tilt the top edge away from the camera"],
		["flip", "Tilt the bottom edge away from the camera"],
		["rotate_90_degrees_ccw", "Rotate it about 45°, like a diamond"],
		["shuffle", "Any new position", "Mix the angle, distance and position. Each view should be different."],
	];

	const $ = (selector, root = document) => root.querySelector(selector);
	const store = {
		get(key) { try { return localStorage.getItem(key); } catch { return null; } },
		set(key, value) { try { localStorage.setItem(key, value); } catch { /* storage unavailable */ } },
		session(key, value) {
			try {
				if (value === undefined) return sessionStorage.getItem(key);
				sessionStorage.setItem(key, value);
			} catch { return null; }
			return value;
		},
	};

	/** Create an element: el("p", {class: "x"}, "text", child). Strings become text, never HTML. */
	function el(tag, attributes = {}, ...children) {
		const element = document.createElement(tag);
		for (const [name, value] of Object.entries(attributes)) {
			if (value === false || value == null) continue;
			if (name.startsWith("on")) element.addEventListener(name.slice(2), value);
			else if (name === "class") element.className = value;
			else element.setAttribute(name, value === true ? "" : value);
		}
		element.append(...children.flat().filter(child => child != null && child !== false));
		return element;
	}
	const icon = name => el("span", { class: "material-symbols-outlined", "aria-hidden": "true" }, name);
	const code = text => el("code", {}, text);
	const usage = camera => camera.robot_id ? `robot.cameras[${JSON.stringify(camera.robot_id)}]` : null;

	async function api(path, body) {
		const response = await fetch(path, body === undefined ? {} : {
			method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
		});
		const data = await response.json().catch(() => ({}));
		return { ok: response.ok, status: response.status, data };
	}

	const stylesheet = el("link", { rel: "stylesheet", href: "/psc-ui.css" });
	document.head.append(stylesheet);

	// ---------------------------------------------------------------- robot state (MQTT)
	const robot = { codeRunning: false, listeners: [] };
	function setCodeRunning(running) {
		if (robot.codeRunning === running) return;
		robot.codeRunning = running;
		robot.listeners.forEach(listener => listener());
	}

	// ---------------------------------------------------------------- toolbar button + banner
	const toolbar = $(".metadata-selectors");
	const dot = el("span", { class: "psc-dot", hidden: true });
	const openButton = el("span", {
		class: "button psc-cal-button", tabindex: "0", role: "button", title: "Camera calibration",
		"aria-label": "Camera calibration", onclick: () => openCalibration(),
		onkeydown: event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openCalibration(); } },
	}, el("span", { class: "icon has-text-link" }, icon("center_focus_strong")), dot);
	if (toolbar) toolbar.prepend(el("div", { class: "control" }, openButton));

	const banner = el("div", { class: "notification is-warning psc-banner", role: "status", hidden: true });
	document.body.append(banner);
	let summary = [];

	function dismissedKey(camera) { return `psc-cal-dismissed:${camera.vidpid}:${camera.robot_id}`; }

	function renderBanner() {
		const needing = summary.filter(camera => !camera.calibrated && camera.robot_id);
		dot.hidden = needing.length === 0;
		const shown = needing.filter(camera => store.session(dismissedKey(camera)) !== "1");
		if (!shown.length || calibration.modal.classList.contains("is-active")) { banner.hidden = true; return; }
		const names = shown.map(camera => camera.name);
		banner.replaceChildren(
			el("button", { class: "delete", "aria-label": "Dismiss", onclick: () => {
				shown.forEach(camera => store.session(dismissedKey(camera), "1"));
				banner.hidden = true;
			} }),
			el("p", {},
				el("strong", {}, shown.length === 1 ? `${names[0]} isn't calibrated.` : `${shown.length} cameras aren't calibrated.`),
				" Robot code can't use a camera to find markers until it's calibrated. It takes about five minutes.",
			),
			el("div", { class: "buttons" },
				el("button", { class: "button is-dark is-small", onclick: () => openCalibration(shown[0]) }, "Calibrate now"),
			),
		);
		banner.hidden = false;
	}

	const importNotice = el("div", { class: "notification is-success psc-banner", role: "status", hidden: true });
	document.body.append(importNotice);
	let importTimer = null;

	function showImports(imports) {
		const fresh = imports.filter(item => store.session(`psc-imported:${item.vidpid}:${item.time}`) !== "1");
		if (!fresh.length) return;
		fresh.forEach(item => store.session(`psc-imported:${item.vidpid}:${item.time}`, "1"));
		importNotice.replaceChildren(
			el("button", { class: "delete", "aria-label": "Dismiss", onclick: () => { importNotice.hidden = true; } }),
			el("p", {}, el("strong", {}, "Camera calibration imported from USB. "),
				fresh.map(item => `${item.name} (from ${item.drive})`).join(", "),
				". Robot code uses it the next time it starts."),
		);
		importNotice.hidden = false;
		clearTimeout(importTimer);
		importTimer = setTimeout(() => { importNotice.hidden = true; }, 15000);
		if (calibration.modal.classList.contains("is-active") && calibration.step === "cameras") loadCameras();
	}

	async function refreshSummary(force = false) {
		if (document.hidden && !force) return;
		const { ok, data } = await api("/api/cameras/summary").catch(() => ({ ok: false }));
		if (!ok) return;
		summary = data.cameras || [];
		showImports(data.imports || []);
		renderBanner();
	}

	// ---------------------------------------------------------------- calibration dialog
	const calibration = {
		modal: null, body: null, foot: null, steps: null,
		step: "cameras", cameras: null, camera: null, session: null, views: [], result: null,
		message: null, busy: false,
	};

	const STEP_NAMES = { cameras: "Choose camera", board: "Print the board", capture: "Capture views", result: "Save" };
	calibration.steps = el("ol", { class: "psc-steps" }, Object.entries(STEP_NAMES).map(([step, name], position) => el("li", { "data-step": step }, `${position + 1}. ${name}`)));
	calibration.body = el("section", { class: "modal-card-body" });
	calibration.foot = el("footer", { class: "modal-card-foot" });
	calibration.modal = el("div", { class: "modal", id: "psc-calibration", role: "dialog", "aria-modal": "true", "aria-labelledby": "psc-cal-title" },
		el("div", { class: "modal-background", onclick: () => closeCalibration() }),
		el("div", { class: "modal-card" },
			el("header", { class: "modal-card-head" },
				el("p", { class: "modal-card-title", id: "psc-cal-title" }, icon("center_focus_strong"), "Camera calibration"),
				el("button", { class: "delete", "aria-label": "Close", onclick: () => closeCalibration() }),
			),
			calibration.body,
			calibration.foot,
		),
	);
	document.body.append(calibration.modal);

	// kit-ui closes every open modal on Escape; stop the camera stream when that happens.
	new MutationObserver(() => {
		if (!calibration.modal.classList.contains("is-active")) { stopPreview(); renderBanner(); }
	}).observe(calibration.modal, { attributes: true, attributeFilter: ["class"] });

	function openCalibration(camera) {
		calibration.modal.classList.add("is-active");
		banner.hidden = true;
		if (camera && calibration.step === "cameras") calibration.wanted = camera;
		if (calibration.step === "cameras") loadCameras(); else render();
	}

	function closeCalibration() {
		calibration.modal.classList.remove("is-active");
		if (location.hash === "#calibrate") history.replaceState(null, "", location.pathname);
	}

	function goTo(step) {
		calibration.step = step;
		calibration.message = null;
		render();
	}

	function render() {
		for (const item of calibration.steps.children) {
			const order = Object.keys(STEP_NAMES);
			item.classList.toggle("is-current", item.dataset.step === calibration.step);
			item.classList.toggle("is-done", order.indexOf(item.dataset.step) < order.indexOf(calibration.step));
		}
		if (calibration.step !== "capture") stopPreview();
		const views = { cameras: renderCameras, board: renderBoard, capture: renderCapture, result: renderResult };
		const [content, footer] = views[calibration.step]();
		calibration.body.replaceChildren(calibration.steps, codeRunningWarning(), content);
		calibration.foot.replaceChildren(...footer);
	}

	function codeRunningWarning() {
		if (!robot.codeRunning || calibration.step === "result") return "";
		return el("div", { class: "notification is-warning is-light" },
			el("strong", {}, "Robot code is running."),
			" It may be using the camera. Stop it with the ", icon("dangerous"), " Kill button before calibrating.",
		);
	}
	robot.listeners.push(() => { if (calibration.modal.classList.contains("is-active")) render(); });

	// Step 1: cameras ------------------------------------------------------------
	async function loadCameras() {
		calibration.step = "cameras";
		calibration.cameras = null;
		render();
		const { ok, data } = await api("/api/cameras").catch(() => ({ ok: false, data: {} }));
		calibration.cameras = ok ? data.cameras : [];
		calibration.message = ok ? null : (data.error || "The camera service isn't responding. Check that psc-camera-calibration is running.");
		const wanted = calibration.wanted && calibration.cameras.find(camera => camera.vidpid === calibration.wanted.vidpid && camera.robot_id === calibration.wanted.robot_id);
		calibration.wanted = null;
		if (wanted && !wanted.calibrated) return chooseCamera(wanted);
		render();
		refreshSummary(true);
	}

	function cameraStatus(camera) {
		if (!/^[0-9a-f]{4}:[0-9a-f]{4}$/.test(camera.vidpid)) {
			return [el("span", { class: "tag is-danger is-light" }, "Can't be used"),
				"This camera doesn't report a USB ID, so robot code can't match a calibration to it."];
		}
		if (camera.read_only) return [el("span", { class: "tag is-success is-light" }, icon("check"), "Built-in calibration"), "Ready to use. No calibration needed."];
		if (camera.calibrated) return [el("span", { class: "tag is-success is-light" }, icon("check"), "Calibrated"), "Ready to use."];
		return [el("span", { class: "tag is-warning" }, "Needs calibration"), "Robot code can't use this camera until it's calibrated."];
	}

	function renderCameras() {
		const cameras = calibration.cameras;
		let list;
		if (cameras === null) {
			list = el("p", {}, "Looking for cameras…");
		} else if (!cameras.length) {
			list = el("div", { class: "notification" }, calibration.message || "No cameras found. Plug in a USB webcam, then select Refresh.");
		} else {
			list = el("div", { class: "psc-camera-list" }, cameras.map(camera => {
				const [tag, note] = cameraStatus(camera);
				const usable = /^[0-9a-f]{4}:[0-9a-f]{4}$/.test(camera.vidpid) && !camera.read_only;
				const sameModel = cameras.filter(other => other.vidpid === camera.vidpid).length > 1;
				return el("div", { class: "psc-camera-row box" },
					el("div", { class: "psc-camera-info" },
						el("div", { class: "psc-camera-name" }, camera.name, " ", tag),
						el("div", { class: "psc-camera-meta" },
							usage(camera) ? ["In robot code: ", code(usage(camera))] : null,
						),
						el("div", { class: "psc-camera-meta" }, note,
							sameModel ? " Cameras of the same model share one calibration." : ""),
					),
					usable ? el("button", {
						class: camera.calibrated ? "button" : "button is-primary",
						disabled: robot.codeRunning,
						onclick: () => chooseCamera(camera),
					}, camera.calibrated ? "Recalibrate" : "Calibrate") : null,
				);
			}));
		}
		return [
			el("div", {},
				typeof calibration.message === "string" && cameras?.length ? el("div", { class: "notification is-danger is-light" }, calibration.message) : "",
				el("p", { class: "mb-4" }, "Calibrating a camera tells robot code how its lens bends the picture, so it can measure how far away markers are. Each camera model only needs it once."),
				list,
				historySection(),
			),
			[
				el("span", {}),
				el("div", { class: "buttons" },
					el("button", { class: "button", onclick: () => loadCameras() }, icon("refresh"), el("span", {}, "Refresh")),
					el("button", { class: "button", onclick: () => closeCalibration() }, "Close"),
				),
			],
		];
	}

	function historySection() {
		const table = el("tbody", {}, el("tr", {}, el("td", { colspan: "5" }, "Loading…")));
		const details = el("details", { class: "psc-history", ontoggle: async () => {
			if (!details.open) return;
			const { ok, data } = await api("/api/calibration/history").catch(() => ({ ok: false }));
			const records = ok ? data.calibrations : [];
			table.replaceChildren(...(records.length ? records.map(record => el("tr", {},
				el("td", {}, record.camera),
				el("td", {}, record.date || "Unknown"),
				el("td", {}, `${record.frames ?? "-"} views`),
				el("td", {}, record.reprojection_error == null ? "-" : `${record.reprojection_error.toFixed(2)} px`),
				el("td", {}, record.status === "accepted"
					? el("a", { href: `/api/calibration/history/download?file=${encodeURIComponent(record.file)}` }, "Download")
					: "Rejected"),
			)) : [el("tr", {}, el("td", { colspan: "5" }, ok ? "No calibrations saved yet." : "History unavailable."))]));
		} },
			el("summary", {}, "Saved calibrations"),
			el("table", { class: "table is-fullwidth is-narrow" }, table),
		);
		return details;
	}

	async function chooseCamera(camera) {
		calibration.camera = camera;
		calibration.session = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
		calibration.views = [];
		calibration.result = null;
		const { ok, data } = await api("/api/calibration/start", { session: calibration.session, index: camera.index });
		if (!ok) { calibration.message = data.error || "Couldn't start calibration."; render(); return; }
		goTo("board");
	}

	// Step 2: board --------------------------------------------------------------
	function renderBoard() {
		const camera = calibration.camera;
		const checked = store.get("psc-board-printed") === "1";
		const next = el("button", { class: "button is-primary", disabled: !checked, onclick: () => goTo("capture") }, "Next: capture views");
		const confirm = el("input", { type: "checkbox", checked, onchange: event => {
			store.set("psc-board-printed", event.target.checked ? "1" : "0");
			next.disabled = !event.target.checked;
		} });
		return [
			el("div", {},
				camera.calibrated ? el("div", { class: "notification is-info is-light" }, `This replaces the current calibration for ${camera.name}.`) : null,
				el("ol", { class: "psc-checklist" },
					el("li", {}, el("div", {},
						el("strong", {}, "Download the calibration board"),
						el("a", { class: "button is-link is-light mt-2", href: "/api/calibration/board", download: "psc-calibration-board.pdf" },
							icon("download"), el("span", {}, "Download board (A4 PDF)")),
					)),
					el("li", {}, el("div", {},
						el("strong", {}, "Print it at actual size"),
						"In the print dialog choose ", el("em", {}, "Actual size"), " or ", el("em", {}, "100%"), ", and turn off ", el("em", {}, "Fit to page"), ". Print it landscape on A4.",
					)),
					el("li", {}, el("div", {},
						el("strong", {}, "Measure one black square"),
						"With a ruler, each black square must be exactly ", el("strong", {}, "40 mm"), " wide. If it isn't, robot code will get distances wrong, so reprint it.",
					)),
					el("li", {}, el("div", {},
						el("strong", {}, "Keep it flat"),
						"Tape it to stiff card or a clipboard. A bent or curled sheet gives a bad calibration.",
					)),
				),
				el("label", { class: "checkbox" }, confirm, " My board is printed and a square measures 40 mm"),
			),
			[
				el("button", { class: "button", onclick: () => loadCameras() }, "Back"),
				next,
			],
		];
	}

	// Step 3: capture ------------------------------------------------------------
	const preview = el("img", { alt: "Live camera picture" });
	const previewBox = el("div", { class: "psc-preview" }, preview);
	let previewIndex = null;

	function startPreview(index) {
		if (previewIndex === index) return;
		previewIndex = index;
		preview.src = `/api/stream?index=${index}&t=${Date.now()}`;
	}
	function stopPreview() {
		if (previewIndex === null) return;
		previewIndex = null;
		preview.src = "data:,";
	}

	function coverage() {
		const cells = new Set();
		let near = false, far = false;
		for (const view of calibration.views) {
			const [x0, y0, x1, y1] = view.box;
			const column = Math.min(2, Math.floor(((x0 + x1) / 2) * 3));
			const row = Math.min(2, Math.floor(((y0 + y1) / 2) * 3));
			cells.add(row * 3 + column);
			const area = (x1 - x0) * (y1 - y0);
			near ||= area > 0.35;
			far ||= area < 0.12;
		}
		return { cells, near, far };
	}

	async function captureView() {
		if (calibration.busy || robot.codeRunning) return;
		calibration.busy = true;
		render();
		const { ok, data } = await api("/api/calibration/capture", {
			session: calibration.session, index: calibration.camera.index, ...BOARD,
		}).catch(() => ({ ok: false, data: {} }));
		calibration.busy = false;
		if (ok) {
			calibration.views.push({ thumbnail: data.thumbnail, box: data.board_box, tags: data.detected_markers, total: data.total_markers });
			calibration.message = ["success", `View ${data.captured} captured: ${data.detected_markers} of ${data.total_markers} tags seen.`];
			previewBox.classList.remove("psc-flash");
			void previewBox.offsetWidth;
			previewBox.classList.add("psc-flash");
		} else if (data.detected_markers != null) {
			calibration.message = ["danger", `Only ${data.detected_markers} of ${BOARD.rows * BOARD.columns} tags were seen (at least ${BOARD.minimum_markers} needed). Hold the board steady and in good light, and make sure it's in the picture.`];
		} else {
			calibration.message = ["danger", data.error || "Couldn't capture from the camera. Is it still plugged in?"];
		}
		render();
	}

	async function removeView(position) {
		const { ok, data } = await api("/api/calibration/remove", { session: calibration.session, view: position + 1 });
		if (!ok) { calibration.message = ["danger", data.error || "Couldn't remove that view."]; render(); return; }
		calibration.views.splice(position, 1);
		calibration.message = null;
		render();
	}

	function renderViews(worst = new Set()) {
		if (!calibration.views.length) return "";
		return el("div", { class: "psc-views" }, calibration.views.map((view, position) => el("figure", { class: `psc-view${worst.has(position) ? " is-worst" : ""}` },
			el("img", { src: view.thumbnail, alt: `View ${position + 1}` }),
			el("button", { class: "delete is-small", "aria-label": `Remove view ${position + 1}`, title: "Remove this view", onclick: () => removeView(position) }),
			el("figcaption", {}, `View ${position + 1} · ${view.tags}/${view.total} tags`),
		)));
	}

	function renderCapture() {
		const camera = calibration.camera;
		startPreview(camera.index);
		const count = calibration.views.length;
		const [poseIcon, poseText, poseHint] = POSES[Math.min(count, POSES.length - 1)];
		const { cells, near, far } = coverage();
		const message = calibration.message;
		const capture = el("button", {
			class: `button is-primary is-medium is-fullwidth${calibration.busy ? " is-loading" : ""}`,
			disabled: robot.codeRunning, onclick: captureView,
		}, icon("photo_camera"), el("span", {}, "Capture view"));
		return [
			el("div", {},
				el("div", { class: "psc-capture" },
					el("div", {}, previewBox,
						el("p", { class: "is-size-7 has-text-grey mt-2" }, `${camera.name}. Move slowly; every tag should be sharp. Press Space to capture.`)),
					el("div", {},
						el("p", { class: "has-text-weight-semibold" }, count < MINIMUM_VIEWS
							? `${count} of ${MINIMUM_VIEWS} views captured`
							: `${count} views captured: enough to save${count < GOOD_VIEWS ? ` (a few more, up to ${GOOD_VIEWS}, improves accuracy)` : ""}`),
						el("progress", { class: `progress ${count >= MINIMUM_VIEWS ? "is-success" : "is-link"} is-small mb-3`, value: String(Math.min(count, GOOD_VIEWS)), max: String(GOOD_VIEWS) }),
						el("div", { class: "psc-next mb-3" }, icon(poseIcon), el("div", {}, el("p", {}, el("strong", {}, "Next: "), poseText), poseHint ? el("small", {}, poseHint) : "")),
						capture,
						el("div", { class: "buttons mt-2" },
							el("button", { class: "button is-small", disabled: !count, onclick: () => removeView(count - 1) }, icon("undo"), el("span", {}, "Undo last")),
						),
						message ? el("div", { class: `notification is-${message[0]} is-light py-2 px-3`, role: "status" }, message[1]) : "",
						el("div", { class: "psc-variety mt-3" },
							el("div", {},
								el("p", { class: "is-size-7 has-text-weight-semibold" }, "Where the board has been"),
								el("div", { class: "psc-coverage", title: "Picture areas the board's centre has visited" },
									[...Array(9).keys()].map(cell => el("div", { class: cells.has(cell) ? "is-covered" : "" }))),
							),
							el("div", { class: "psc-chips" },
								el("span", { class: `tag ${near ? "is-success" : ""} is-light` }, near ? icon("check") : "", "Close up"),
								el("span", { class: `tag ${far ? "is-success" : ""} is-light` }, far ? icon("check") : "", "Far away"),
								el("span", { class: `tag ${cells.size >= 7 ? "is-success" : ""} is-light` }, cells.size >= 7 ? icon("check") : "", "Edges and corners"),
							),
						),
					),
				),
				renderViews(),
			),
			[
				el("button", { class: "button", onclick: () => goTo("board") }, "Back"),
				el("button", { class: "button is-primary", disabled: count < MINIMUM_VIEWS, onclick: finish },
					count < MINIMUM_VIEWS ? `Capture ${MINIMUM_VIEWS - count} more` : "Calculate and save"),
			],
		];
	}

	document.addEventListener("keydown", event => {
		if (event.code !== "Space" || calibration.step !== "capture" || !calibration.modal.classList.contains("is-active")) return;
		if (event.target.closest?.("input, textarea, select, button, a")) return;
		event.preventDefault();
		captureView();
	});

	// Step 4: result -------------------------------------------------------------
	async function finish() {
		const camera = calibration.camera;
		calibration.result = { pending: true };
		goTo("result");
		const { ok, data } = await api("/api/calibration/finish", {
			session: calibration.session, index: camera.index, vidpid: camera.vidpid,
			resolution: [camera.width, camera.height], minimum_frames: MINIMUM_VIEWS, ...BOARD,
		}).catch(() => ({ ok: false, data: {} }));
		calibration.result = { ok, data };
		render();
		if (ok) refreshSummary(true);
	}

	function rating(error) {
		if (error < 0.5) return ["Excellent", "is-success"];
		if (error < 1) return ["Good", "is-success"];
		return ["Acceptable", "is-warning"];
	}

	function renderResult() {
		const camera = calibration.camera;
		const result = calibration.result;
		if (!result || result.pending) {
			return [el("div", { class: "has-text-centered py-6" }, el("p", { class: "mb-3" }, "Calculating the calibration…"), el("progress", { class: "progress is-small is-link" })), []];
		}
		const { ok, data } = result;
		if (ok) {
			const [label, colour] = rating(data.reprojection_error);
			return [
				el("div", {},
					el("div", { class: "notification is-success is-light" }, el("strong", {}, `Calibration saved for ${camera.name}.`)),
					el("div", { class: "columns" },
						el("div", { class: "column is-narrow" },
							el("div", { class: "psc-result-figure" }, label),
							el("span", { class: `tag ${colour} is-light` }, `${data.reprojection_error.toFixed(2)} px average error`),
							el("p", { class: "is-size-7 has-text-grey mt-1" }, `from ${data.captured_frames} views`)),
						el("div", { class: "column content" },
							el("p", {}, el("strong", {}, "What happens now")),
							el("ul", {},
								el("li", {}, "Robot code uses this camera the next time it starts. Restart your code if it's running."),
								data.saved_to_usb?.length
									? el("li", {}, `Also saved to the USB drive (${data.saved_to_usb.join(", ")}), in the camera-calibrations folder. Plugging that drive into another robot imports it automatically.`)
									: el("li", {}, "No robot code USB drive was plugged in, so it was only saved on the robot."),
								usage(camera) ? el("li", {}, "Use it in code as ", code(usage(camera)), ", or ", code("robot.camera"), " if it's the only camera.") : null,
								el("li", {}, "Other cameras of the same model use this calibration too."),
							),
						),
					),
				),
				[
					el("a", { class: "button", href: `/api/calibration/download?session=${encodeURIComponent(calibration.session)}` }, icon("download"), el("span", {}, "Download calibration file")),
					el("button", { class: "button is-primary", onclick: () => loadCameras() }, "Done"),
				],
			];
		}
		if (data.reprojection_error != null) {
			const diagnostics = (data.per_view_diagnostics || []).slice().sort((a, b) => b.reprojection_rms_px - a.reprojection_rms_px);
			const worst = new Set(diagnostics.filter(view => view.reprojection_rms_px > MAX_ERROR_PX).slice(0, 3).map(view => view.view - 1));
			return [
				el("div", {},
					el("div", { class: "notification is-danger is-light" },
						el("strong", {}, `Not accurate enough to save (${data.reprojection_error.toFixed(2)} px; ${MAX_ERROR_PX} px or less is needed).`),
						" Nothing has been saved."),
					el("div", { class: "content" },
						el("p", {}, el("strong", {}, "Common causes")),
						el("ul", {},
							el("li", {}, "The board wasn't printed at 100%: check a square measures 40 mm."),
							el("li", {}, "The sheet was bent or curled."),
							el("li", {}, "Some views were blurry: hold the board still and use good light."),
						),
						worst.size ? el("p", {}, "The views outlined in red fit worst. Remove them, capture a few new views, then try again.") : null,
					),
					renderViews(worst),
				),
				[
					el("button", { class: "button", onclick: () => chooseCamera(camera) }, "Start over"),
					el("button", { class: "button is-primary", onclick: () => goTo("capture") }, "Back to capturing"),
				],
			];
		}
		return [
			el("div", { class: "notification is-danger is-light" }, data.error || "The calibration couldn't be calculated."),
			[el("button", { class: "button", onclick: () => goTo("capture") }, "Back to capturing")],
		];
	}

	// ---------------------------------------------------------------- multi-camera view
	const cameraModal = document.getElementById("modal-camera");
	const legacyImage = document.getElementById("last-annotated-image");
	const instructions = document.getElementById("no-annotated-image-instructions");
	const tiles = new Map();
	let grid = null, singleCaption = null, cameraCard = null, titleText = null;

	if (cameraModal && legacyImage) {
		cameraCard = $(".modal-card", cameraModal);
		const title = $(".modal-card-title", cameraModal);
		titleText = [...title.childNodes].find(node => node.nodeType === Node.TEXT_NODE && node.textContent.trim());
		if (instructions) {
			instructions.replaceChildren("Use ", code("robot.camera.see()"), " in your robot code to see an annotated image. With several cameras, use ", code('robot.cameras["<name>"].see()'), ".");
		}
		grid = el("div", { id: "psc-camera-grid" });
		singleCaption = el("p", { class: "psc-camera-caption", hidden: true });
		legacyImage.after(singleCaption, grid);
	}

	function layoutCameras() {
		if (!grid) return;
		const multi = tiles.size > 1;
		grid.classList.toggle("is-multi", multi);
		cameraCard.classList.toggle("psc-camera-wide", multi);
		legacyImage.style.display = multi ? "none" : "";
		singleCaption.hidden = tiles.size !== 1;
		if (tiles.size === 1) singleCaption.replaceChildren("Camera: ", code(`robot.cameras[${JSON.stringify([...tiles.keys()][0])}]`));
		if (titleText) titleText.textContent = multi ? ` Last Images Captured (${tiles.size} cameras)` : " Last Image Captured";
	}

	function showFrame(cameraId, data) {
		if (!grid) return;
		let tile = tiles.get(cameraId);
		if (!tile) {
			const image = el("img", { alt: `Last image from ${cameraId}` });
			const figure = el("figure", {}, image, el("figcaption", { class: "psc-camera-caption" }, code(`robot.cameras[${JSON.stringify(cameraId)}]`)));
			tile = { figure, image };
			tiles.set(cameraId, tile);
			const order = [...tiles.keys()].sort();
			const next = order[order.indexOf(cameraId) + 1];
			grid.insertBefore(figure, next ? tiles.get(next).figure : null);
			layoutCameras();
		}
		tile.image.src = data;
	}

	// Live view of every camera while robot code isn't running. The calibration
	// service also stops these streams itself when code starts.
	const liveBox = el("div", { id: "psc-live", hidden: true });
	if (cameraModal) $(".modal-card-body", cameraModal).prepend(liveBox);
	let liveOn = false;

	function stopLive() {
		liveBox.querySelectorAll("img").forEach(image => { image.src = "data:,"; });
		liveBox.replaceChildren();
		liveBox.hidden = true;
		cameraModal?.classList.remove("psc-live-on");
		cameraCard?.classList.remove("psc-camera-wide");
		liveOn = false;
		layoutCameras();
	}

	async function updateLive() {
		const wanted = cameraModal?.classList.contains("is-active") && !robot.codeRunning;
		if (!wanted) { if (liveOn) stopLive(); return; }
		if (liveOn) return;
		liveOn = true;
		const { ok, data } = await api("/api/cameras/summary").catch(() => ({ ok: false, data: {} }));
		if (!liveOn || robot.codeRunning) return;
		const cameras = ok ? data.cameras || [] : [];
		cameraModal.classList.add("psc-live-on");
		cameraCard?.classList.toggle("psc-camera-wide", cameras.length > 1);
		liveBox.replaceChildren(
			el("p", { class: "psc-camera-caption psc-live-note" }, icon("videocam"),
				cameras.length
					? " Live view. Robot code isn't running; when it runs, this shows the last image it took."
					: " No cameras connected."),
			el("div", { class: "psc-live-grid" }, cameras.map(camera => el("figure", {},
				el("img", { src: `/api/stream?index=${camera.index}&t=${Date.now()}`, alt: `Live view from ${camera.name}` }),
				el("figcaption", { class: "psc-camera-caption" },
					camera.robot_id ? code(`robot.cameras[${JSON.stringify(camera.robot_id)}]`) : camera.name,
					camera.calibrated ? "" : " (not calibrated)"),
			))),
		);
		liveBox.hidden = false;
		if (titleText) titleText.textContent = " Live View";
	}

	if (cameraModal) new MutationObserver(updateLive).observe(cameraModal, { attributes: true, attributeFilter: ["class"] });
	robot.listeners.push(updateLive);

	function clearCameras() {
		tiles.clear();
		grid?.replaceChildren();
		layoutCameras();
	}

	// ---------------------------------------------------------------- MQTT over WebSocket
	// Minimal MQTT 3.1.1 client (QoS 0 subscribe only), so the page works offline on
	// the robot's network without loading an MQTT library.
	function onMessage(topic, text) {
		let message;
		try { message = JSON.parse(text); } catch { return; }
		if (topic === "astoria/astprocd") {
			if (message.code_status === "code_starting") clearCameras();
			setCodeRunning(["code_starting", "code_running"].includes(message.code_status));
			return;
		}
		if (typeof message.camera !== "string" || typeof message.data !== "string") return;
		if (!message.data.startsWith("data:image/")) return;
		showFrame(message.camera, message.data);
	}

	const encoder = new TextEncoder();
	const decoder = new TextDecoder();
	let socket = null, buffer = new Uint8Array(0), pingTimer = null;

	function mqttString(text) {
		const bytes = encoder.encode(text);
		return [bytes.length >> 8, bytes.length & 255, ...bytes];
	}

	function packet(header, bodyBytes) {
		const length = [];
		let remaining = bodyBytes.length;
		do {
			let digit = remaining % 128;
			remaining = Math.floor(remaining / 128);
			if (remaining > 0) digit |= 128;
			length.push(digit);
		} while (remaining > 0);
		return new Uint8Array([header, ...length, ...bodyBytes]);
	}

	function readPackets() {
		for (;;) {
			if (buffer.length < 2) return;
			let length = 0, multiplier = 1, offset = 1, digit;
			do {
				if (offset >= buffer.length) return;
				digit = buffer[offset++];
				length += (digit & 127) * multiplier;
				multiplier *= 128;
			} while (digit & 128);
			if (buffer.length < offset + length) return;
			const header = buffer[0];
			const payload = buffer.subarray(offset, offset + length);
			buffer = buffer.slice(offset + length);
			if (header >> 4 !== 3) continue; // only PUBLISH carries messages
			const topicLength = (payload[0] << 8) | payload[1];
			const qos = (header >> 1) & 3;
			const topic = decoder.decode(payload.subarray(2, 2 + topicLength));
			onMessage(topic, decoder.decode(payload.subarray(2 + topicLength + (qos ? 2 : 0))));
		}
	}

	function connect() {
		socket = new WebSocket(`ws://${location.hostname}:9001`, ["mqtt"]);
		socket.binaryType = "arraybuffer";
		socket.onopen = () => {
			const clientId = `psc-ui-${Math.random().toString(16).slice(2, 10)}`;
			socket.send(packet(0x10, [...mqttString("MQTT"), 4, 0x02, 0, 60, ...mqttString(clientId)]));
			socket.send(packet(0x82, [0, 1, ...mqttString("camera/annotated"), 0, ...mqttString("astoria/astprocd"), 0]));
			pingTimer = setInterval(() => socket.readyState === WebSocket.OPEN && socket.send(new Uint8Array([0xc0, 0])), 30000);
		};
		socket.onmessage = event => {
			const chunk = new Uint8Array(event.data);
			const joined = new Uint8Array(buffer.length + chunk.length);
			joined.set(buffer);
			joined.set(chunk, buffer.length);
			buffer = joined;
			readPackets();
		};
		socket.onclose = () => {
			clearInterval(pingTimer);
			buffer = new Uint8Array(0);
			setTimeout(connect, 3000);
		};
	}

	// ---------------------------------------------------------------- gamepads dialog
	const PAIRING_STEPS = [
		["Xbox Wireless Controller", "Turn it on with the Xbox button, then hold the small pair button on the top (next to the USB port) until the Xbox button flashes quickly."],
		["PlayStation 4 (DualShock 4)", "Hold Share and the PS button together until the light bar flashes twice, again and again."],
		["PlayStation 5 (DualSense)", "Hold Create and the PS button together until the lights around the touchpad flash."],
		["Google Stadia", "It must already be switched to Bluetooth mode (a one-off step using Google's Stadia Bluetooth tool). Then hold the Stadia button and Y for 2 seconds, until the light flashes orange."],
		["Nintendo Switch Pro Controller", "Hold the small sync button on the top until the player lights move back and forth."],
		["8BitDo and other controllers", "Most enter pairing mode by holding their pair button for about 3 seconds. Check the controller's manual."],
	];
	const BUTTON_LABELS = {
		a: "A", b: "B", x: "X", y: "Y", left_bumper: "LB", right_bumper: "RB", back: "Back", start: "Start",
		guide: "Home", left_stick_button: "LS", right_stick_button: "RS",
		dpad_up: "Up", dpad_down: "Down", dpad_left: "Left", dpad_right: "Right",
	};
	const pads = { status: null, input: [], error: null, inputTimer: null, statusTimer: null, howToOpen: false };
	const padDot = el("span", { class: "psc-dot is-connected", hidden: true });
	const padButton = el("span", {
		class: "button psc-cal-button", tabindex: "0", role: "button", title: "Gamepads",
		"aria-label": "Gamepads", onclick: () => openGamepads(),
		onkeydown: event => { if (event.key === "Enter" || event.key === " ") { event.preventDefault(); openGamepads(); } },
	}, el("span", { class: "icon has-text-link" }, icon("sports_esports")), padDot);
	if (toolbar) toolbar.prepend(el("div", { class: "control" }, padButton));

	pads.body = el("section", { class: "modal-card-body" });
	pads.modal = el("div", { class: "modal", id: "psc-gamepads", role: "dialog", "aria-modal": "true", "aria-labelledby": "psc-pad-title" },
		el("div", { class: "modal-background", onclick: () => closeGamepads() }),
		el("div", { class: "modal-card" },
			el("header", { class: "modal-card-head" },
				el("p", { class: "modal-card-title", id: "psc-pad-title" }, icon("sports_esports"), "Gamepads"),
				el("button", { class: "delete", "aria-label": "Close", onclick: () => closeGamepads() }),
			),
			pads.body,
			el("footer", { class: "modal-card-foot" }, el("button", { class: "button", onclick: () => closeGamepads() }, "Close")),
		),
	);
	document.body.append(pads.modal);
	const padsOpen = () => pads.modal.classList.contains("is-active");
	new MutationObserver(() => { if (!padsOpen()) stopGamepadPolling(); })
		.observe(pads.modal, { attributes: true, attributeFilter: ["class"] });

	function openGamepads() {
		pads.modal.classList.add("is-active");
		pads.shown = null;
		renderGamepads();
		refreshGamepads();
		clearInterval(pads.statusTimer);
		pads.statusTimer = setInterval(refreshGamepads, 1000);
		clearInterval(pads.inputTimer);
		pads.inputTimer = setInterval(refreshInput, 100);
	}

	function closeGamepads() {
		pads.modal.classList.remove("is-active");
		if (location.hash === "#gamepads") history.replaceState(null, "", location.pathname);
	}

	function stopGamepadPolling() {
		clearInterval(pads.statusTimer);
		clearInterval(pads.inputTimer);
		pads.statusTimer = pads.inputTimer = null;
	}

	async function refreshGamepads() {
		const { ok, data } = await api("/gamepad-api/status").catch(() => ({ ok: false, data: {} }));
		pads.error = ok ? null : "The gamepad service isn't responding. Check that psc-gamepadd is running.";
		if (ok) {
			pads.status = data;
			pads.input = data.gamepads || [];
		}
		padDot.hidden = !(pads.input.length);
		if (!padsOpen()) return;
		// Only redraw when something shown has changed: redrawing replaces the buttons,
		// and a click that lands during a redraw would be lost. Live input is drawn
		// separately, by updateInputViews.
		const shown = JSON.stringify([pads.error, pads.howToOpen, pads.status && {
			...pads.status, gamepads: pads.input.map(pad => [pad.player, pad.name, pad.address]),
			devices: pads.status.devices.map(({ rssi, job, ...device }) => ({ ...device, job: job && [job.state, job.message] })),
		}]);
		if (shown === pads.shown) return;
		pads.shown = shown;
		renderGamepads();
	}

	let inputBusy = false;
	async function refreshInput() {
		if (inputBusy || !pads.input.length) return;
		inputBusy = true;
		const { ok, data } = await api("/gamepad-api/input").catch(() => ({ ok: false }));
		inputBusy = false;
		if (!ok || !padsOpen()) return;
		pads.input = data.gamepads || [];
		updateInputViews();
	}

	async function padAction(path, body, failure) {
		const { ok, data } = await api(`/gamepad-api/${path}`, body).catch(() => ({ ok: false, data: {} }));
		if (!ok) pads.error = data.error || failure;
		await refreshGamepads();
	}

	function stick(x, y) {
		const dot = el("span", { class: "psc-stick-dot" });
		dot.style.left = `${50 + x * 42}%`;
		dot.style.top = `${50 - y * 42}%`;
		return el("span", { class: "psc-stick" }, dot);
	}

	function bar(value) {
		const fill = el("span", { class: "psc-bar-fill" });
		fill.style.width = `${Math.round(value * 100)}%`;
		return el("span", { class: "psc-bar" }, fill);
	}

	function inputView(pad) {
		return el("div", { class: "psc-pad", "data-player": pad.player },
			el("div", { class: "psc-pad-head" },
				el("strong", {}, pad.name || "Gamepad"),
				el("span", { class: "tag is-link is-light" }, `Player ${pad.player}`),
				pad.address ? "" : el("span", { class: "tag is-light" }, "USB"),
			),
			el("div", { class: "psc-pad-controls" },
				el("figure", {}, stick(pad.left_x, pad.left_y), el("figcaption", {}, "Left stick")),
				el("figure", {}, stick(pad.right_x, pad.right_y), el("figcaption", {}, "Right stick")),
				el("div", { class: "psc-triggers" },
					el("label", {}, "LT", bar(pad.left_trigger)),
					el("label", {}, "RT", bar(pad.right_trigger)),
				),
			),
			el("div", { class: "psc-pad-buttons" }, Object.entries(BUTTON_LABELS).map(([name, label]) =>
				el("span", { class: `tag ${pad.pressed.includes(name) ? "is-success" : "is-light"}`, "data-button": name }, label))),
		);
	}

	function updateInputViews() {
		const list = $("#psc-pad-inputs", pads.body);
		if (!list) return;
		const players = pads.input.map(pad => String(pad.player)).join();
		if (list.dataset.players !== players) {
			list.dataset.players = players;
			list.replaceChildren(...pads.input.map(inputView));
			return;
		}
		for (const pad of pads.input) {
			const view = list.querySelector(`[data-player="${pad.player}"]`);
			if (view) view.replaceWith(inputView(pad));
		}
	}

	function deviceRow(device) {
		const job = device.job;
		const busy = job && (job.state === "pairing" || job.state === "connecting");
		let state;
		if (busy) state = el("span", { class: "tag is-info is-light" }, job.message || "Pairing...");
		else if (device.connected) state = el("span", { class: "tag is-success is-light" }, icon("check"), "Connected");
		else if (device.paired) state = el("span", { class: "tag is-light" }, "Not connected");
		const actions = device.paired
			? [
				device.connected ? el("button", { class: "button is-small", onclick: () => padAction("disconnect", { address: device.address }, "Couldn't disconnect.") }, "Disconnect") : "",
				el("button", { class: "button is-small is-danger is-outlined", onclick: () => {
					if (confirm(`Forget ${device.name}? You'll need to pair it again to use it.`)) padAction("forget", { address: device.address }, "Couldn't forget it.");
				} }, "Forget"),
			]
			: [el("button", { class: `button is-small is-link${busy ? " is-loading" : ""}`, disabled: busy || pads.status.competition_blocked,
				onclick: () => padAction("pair", { address: device.address }, "Couldn't start pairing.") }, "Pair")];
		return el("div", { class: "psc-camera-row" },
			el("span", { class: "icon" }, icon(device.gamepad ? "sports_esports" : "bluetooth")),
			el("div", { class: "psc-camera-info" },
				el("div", { class: "psc-camera-name" }, device.name),
				el("div", { class: "psc-camera-meta" }, state || "", " ",
					job && job.state === "failed" ? el("span", { class: "has-text-danger" }, job.message) : "",
					!busy && device.paired && !device.connected && !(job && job.state === "failed") ? "Turn the controller on, or press its home button, to connect it." : ""),
			),
			el("div", { class: "buttons" }, actions),
		);
	}

	function renderGamepads() {
		const status = pads.status;
		const parts = [];
		if (pads.error) parts.push(el("div", { class: "notification is-danger is-light" }, pads.error));
		if (!status) {
			parts.push(el("p", {}, "Loading..."));
			pads.body.replaceChildren(...parts);
			return;
		}
		const bluetooth = status.bluetooth;
		if (status.competition_blocked) {
			parts.push(el("div", { class: "notification is-warning is-light" },
				el("strong", {}, "Bluetooth is switched off by the competition USB."),
				" Bluetooth gamepads can't be used during this match. USB gamepads still work."));
		} else if (!bluetooth.present) {
			parts.push(el("div", { class: "notification is-info is-light" },
				"Bluetooth is starting. If this doesn't go away after a minute, restart the robot."));
		}

		parts.push(el("h3", { class: "title is-6" }, "Connected gamepads"));
		parts.push(pads.input.length
			? el("div", { id: "psc-pad-inputs", class: "psc-pad-list" })
			: el("p", { class: "psc-muted" }, "No gamepad is connected. Pair a Bluetooth controller below, or plug one in with a USB cable."));

		const paired = status.devices.filter(device => device.paired);
		if (paired.length) {
			parts.push(el("h3", { class: "title is-6" }, "Paired controllers"));
			parts.push(el("div", { class: "psc-camera-list" }, paired.map(deviceRow)));
		}

		const nearby = status.devices.filter(device => !device.paired);
		const searching = bluetooth.searching;
		parts.push(el("h3", { class: "title is-6" }, "Pair a controller"));
		parts.push(el("ol", { class: "psc-checklist" },
			el("li", {}, el("div", {}, el("strong", {}, "Put the controller in pairing mode."),
				el("details", { class: "psc-history", open: pads.howToOpen, ontoggle: event => { pads.howToOpen = event.target.open; } },
					el("summary", {}, "How to do this for your controller"),
					el("dl", { class: "psc-pairing" }, PAIRING_STEPS.map(([name, text]) => [el("dt", {}, name), el("dd", {}, text)]))))),
			el("li", {}, el("div", {}, el("strong", {}, "Search, then pair it."),
				el("div", { class: "buttons" },
					el("button", { class: `button is-link${searching ? " is-loading" : ""}`, disabled: !bluetooth.powered || status.competition_blocked,
						onclick: () => padAction("scan", { on: true }, "Couldn't start searching.") }, icon("search"), "Search"),
					searching ? el("span", { class: "psc-muted" }, "Searching for 45 seconds...") : ""))),
		));
		if (nearby.length) parts.push(el("div", { class: "psc-camera-list" }, nearby.map(deviceRow)));
		else if (searching) parts.push(el("p", { class: "psc-muted" }, "Nothing found yet. Check the controller is in pairing mode."));

		parts.push(el("p", { class: "psc-muted psc-pad-code" }, "In your code, use ", code("robot.wait_for_gamepad()"), " and ", code("robot.gamepad"), "."));
		pads.body.replaceChildren(...parts);
		updateInputViews();
	}

	// ---------------------------------------------------------------- start
	layoutCameras();
	connect();
	refreshSummary(true);
	setInterval(() => refreshSummary(), 5000);
	document.addEventListener("visibilitychange", () => refreshSummary());
	if (location.hash === "#calibrate") openCalibration();
	if (location.hash === "#gamepads") openGamepads();
	refreshGamepads();
	setInterval(() => { if (!padsOpen() && !document.hidden) refreshGamepads(); }, 10000);
})();

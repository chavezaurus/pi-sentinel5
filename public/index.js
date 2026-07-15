"use strict";

const {
  body,
  main,
  nav,
  button,
  fieldset,
  div,
  form,
  h2,
  h3,
  input,
  label,
  option,
  section,
  select,
  source,
  span,
  table,
  tbody,
  th,
  thead,
  tr,
  td,
  p,
  colgroup,
  col,
  video,
  img,
  ul,
  li,
  a,
  details,
  summary,
  dialog,
} = van.tags;

const eventArray = van.state([]);
const calibrationEvents = van.state([]);
const calibrationParameters = van.state({});
const eventId = van.state({ dir: "", id: "" });
const showStars = van.state(false);
const calMode = van.state("params");
const starObjects = van.state([]);
const svgStarString = van.state("");
const skySamples = van.state([]);
const selectedMode = van.state("");
const confirmText = van.state("");
const promptText = van.state("");
const promptValue = van.state("");

let tableSelection = "new";
let goodVideo = false;
let goodImage = false;
let videoToggle = false;
let changeToken = 0;

const get = async (url, options = {}) => {
  const response = await fetch(url, {
    method: "GET",
    ...options,
  });

  if (!response.ok) {
    throw new Error(`GET ${url} failed: ${response.status}`);
  }

  return response.json();
};

const post = async (url, data, options = {}) => {
  const response = await fetch(url, {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      ...(options.headers || {}),
    },
    body: JSON.stringify(data),
    ...options,
  });

  if (!response.ok) {
    throw new Error(`POST ${url} failed: ${response.status}`);
  }

  return response.json();
};

async function subscribe() {
  try {
    let response = await fetch("/subscribe");

    if (response.status == 502) {
      // Status 502 is a connection timeout error,
      // may happen if the connection was pending for too long
      // and the remote server or a proxy closed it
      await subscribe();
    } else if (response.status != 200) {
      // An error occurred, let's show it
      // Reconnect in one second
      await new Promise((resolve) => setTimeout(resolve, 1000));
      await subscribe();
    } else {
      // Get and show the message
      let result = await response.json();
      if (result.msg === "changed") {
        requestControls();
      }
      // Call subscribe() again to get the next message
      await subscribe();
    }
  } catch (e) {
    // Reconnect in one second on network error
    await new Promise((resolve) => setTimeout(resolve, 1000));
    await subscribe();
  }
}

//
// Do things when the mode changes
//
van.derive(() => {
  let v = selectedMode.val;
  let c = calMode.val;

  if (v == "" || c == "") return;

  form1.style.display = v == "controls" ? "block" : "none";
  tableSelect.style.display = v == "events" ? "flex" : "none";
  eventTable.style.display = v == "events" ? "block" : "none";
  calButtons.style.display = v == "cals" ? "flex" : "none";

  let cmode = v == "cals";

  form2.style.display = cmode && c == "params" ? "block" : "none";
  starTable.style.display = cmode && c == "images" ? "block" : "none";
  skyObjectTable.style.display = cmode && c == "samples" ? "block" : "none";
});

const affineState = van.derive(() => {
  let af = {};
  let calParams = calibrationParameters.val;

  const flat = calParams.flat;
  const COPx = calParams.COPx;
  const COPy = calParams.COPy;
  const alpha = calParams.alpha;
  const dilation = Math.sqrt(1 - Math.min(flat, 0.999));
  const sin_alpha = Math.sin(alpha);
  const cos_alpha = Math.cos(alpha);
  const K = COPx * sin_alpha + COPy * cos_alpha;
  const L = COPy * sin_alpha - COPx * cos_alpha;
  const c =
    cos_alpha * cos_alpha * dilation + (sin_alpha * sin_alpha) / dilation;
  const d =
    sin_alpha * cos_alpha * dilation - (sin_alpha * cos_alpha) / dilation;
  const e =
    -(K * cos_alpha * dilation * dilation - COPy * dilation + L * sin_alpha) /
    dilation;
  const f =
    sin_alpha * cos_alpha * dilation - (sin_alpha * cos_alpha) / dilation;
  const g =
    sin_alpha * sin_alpha * dilation + (cos_alpha * cos_alpha) / dilation;
  const h =
    -(K * sin_alpha * dilation * dilation - COPx * dilation - L * cos_alpha) /
    dilation;

  af.c = c;
  af.d = d;
  af.e = e;
  af.f = f;
  af.g = g;
  af.h = h;

  return af;
});

const eventTime = van.derive(() => {
  const s = eventId.val.id;

  if (s == "") return new Date();

  const year = Number(s.substring(1, 5));
  const month = Number(s.substring(5, 7)) - 1;
  const day = Number(s.substring(7, 9));
  const hour = Number(s.substring(10, 12));
  const minute = Number(s.substring(12, 14));
  const second = Number(s.substring(14, 16));
  const millisec = Number(s.substring(17, 20));

  const date = new Date(
    Date.UTC(year, month, day, hour, minute, second, millisec),
  );
  return date;
});

const jpgPath = van.derive(() => {
  const eid = eventId.val;
  const base = eid.id;
  const dir = eid.dir;
  if (base == "") return "#";

  return dir + "/" + base + ".jpg";
});

const mp4Path = van.derive(() => {
  const eid = eventId.val;
  const base = eid.id;
  const dir = eid.dir;
  if (base == "") return "#";

  return dir + "/" + base + ".mp4";
});

const starClasses = van.derive(() => {
  return showStars.val
    ? "responsive starImage"
    : "responsive starImage invisible";
});

const formattedDate = van.derive(() => {
  const date = eventTime.val;

  const year = date.getFullYear();
  // Add 1 to the month (0-indexed) and pad with a leading zero if needed
  const month = (date.getMonth() + 1).toString().padStart(2, "0");
  const day = date.getDate().toString().padStart(2, "0");

  const formattedDate = `${year}-${month}-${day}`;
  return formattedDate;
});

const formattedTime = van.derive(() => {
  const date = eventTime.val;

  const hours = date.getHours().toString().padStart(2, "0");
  const minutes = date.getMinutes().toString().padStart(2, "0");
  const seconds = date.getSeconds().toString().padStart(2, "0");

  const formattedTime = `${hours}:${minutes}:${seconds}`;

  return formattedTime;
});

const formattedHourMinute = van.derive(() => {
  const date = eventTime.val;

  const hours = date.getHours().toString().padStart(2, "0");
  const minutes = date.getMinutes().toString().padStart(2, "0");

  const formattedHourMinute = `${hours}:${minutes}`;
  return formattedHourMinute;
});

//
// Used to determine if the image is shown or the movie is played.
//
van.derive(() => {
  const eid = eventId.val;
  const id = eid.id;
  const oldId = eventId.oldVal.id;
  const dir = eid.dir;

  if (id == "") return;

  if (id != oldId) {
    if (goodVideo) {
      if (videoToggle) {
        videoPlayer1.src = dir + "/" + id + ".mp4";
      } else {
        videoPlayer2.src = dir + "/" + id + ".mp4";
      }
      videoToggle = !videoToggle;
    }
    if (goodImage) imagePlayer.src = dir + "/" + id + ".jpg";

    imagePlayer.classList.remove("invisible");
  } else {
    imagePlayer.classList.toggle("invisible");
  }

  if (!goodImage) imagePlayer.classList.add("invisible");

  if (!goodVideo) imagePlayer.classList.remove("invisible");

  if (imagePlayer.classList.contains("invisible")) {
    if (videoToggle) {
      videoPlayer2.play();
    } else {
      videoPlayer1.play();
    }
  }
});

//
// Used to get star positions and convert them to pixel locations
//
van.derive(() => {
  const params = calibrationParameters.val;
  const event = eventId.rawVal;

  if (event.id != "") getStars();
});

van.derive(() => {
  const oldEvent = eventId.oldVal;
  const params = calibrationParameters.rawVal;

  if (Object.keys(params).length === 0) return;

  if (eventId.id != oldEvent.id) getStars();
});

async function waitForCompletion() {
  try {
    const result = await get("get_running");
    if (result.response !== "Idle") {
      setTimeout(waitForCompletion, 3000);
    } else {
      alert("Action Completed");
    }
  } catch (error) {
    alert(error);
  }
}

async function getStars() {
  const params = calibrationParameters.rawVal;
  const event = eventId.rawVal;

  if (event.id === "") return;

  const obj = {
    path: event.id,
    cameraLatitude: params.cameraLatitude,
    cameraLongitude: params.cameraLongitude,
    cameraElevation: params.cameraElevation,
  };

  try {
    const data = await post("stars", obj);
    if (data.response != "OK") {
      alert("No Stars");
      return;
    }

    let stars = data.sky_objects;
    starObjects.val = stars.map(Transform);
  } catch (error) {
    alert(error);
  }
}

van.derive(async () => {
  const params = calibrationParameters.val;
  const event = eventId.val;
  const oldEvent = eventId.oldVal;

  if (Object.keys(params).length === 0) return;

  if (event.id === "") return;

  if (event.id === oldEvent.id) return;

  const obj = {
    path: event.id,
    cameraLatitude: params.cameraLatitude,
    cameraLongitude: params.cameraLongitude,
    cameraElevation: params.cameraElevation,
  };

  try {
    const data = await post("stars", obj);
    if (data.response != "OK") {
      alert("No Stars");
      return;
    }

    let stars = data.sky_objects;
    starObjects.val = stars.map(Transform);
  } catch (error) {
    alert(error);
  }
});

//
// Used to take star positions, along with cardinal directions,
//  and convert them to svg derived images.
//
van.derive(() => {
  const stars = starObjects.val;

  const directions = [];
  directions.push({ azim: 0.0, elev: 0.0, name: "N" });
  directions.push({ azim: 90.0, elev: 0.0, name: "E" });
  directions.push({ azim: 180.0, elev: 0.0, name: "S" });
  directions.push({ azim: 270.0, elev: 0.0, name: "W" });
  directions.push({ azim: 0.0, elev: 90.0, name: "Z" });

  let dPixels = directions.map(Transform);
  let pixels = dPixels.concat(stars);

  let strings = [];
  strings.push(
    `<svg xmlns="http://www.w3.org/2000/svg" width="1920" height="1080">`,
  );

  let lightStrings = pixels.map(svgRender);
  let lightText = pixels.map(svgTextRender);

  strings.push(...lightStrings);
  strings.push(...lightText);
  strings.push("</svg>");

  let sss = strings.join(" ");

  const encoded = encodeURIComponent(sss);
  svgStarString.val = `data:image/svg+xml;charset=utf-8,${encoded}`;
});

function PixelToAzEl(px, py) {
  // Apply the affine transformation to the input coordinates
  // PURPOSE: Correct elliptical image distortion

  const affine = affineState.rawVal;
  const calParams = calibrationParameters.rawVal;

  const c = affine.c;
  const d = affine.d;
  const e = affine.e;
  const f = affine.f;
  const g = affine.g;
  const h = affine.h;

  const pxt = g * px + f * py + h;
  const pyt = d * px + c * py + e;

  // Now apply the Borovicka calibration equations
  // NOTE 1: The equation for "b" employs atan2(x,y), which is equivalent to (in Excel) atan2(y,x) = atan2(X,Y)
  // NOTE 2: The equation set yields azimuth measured from cardinal SOUTH

  const x = pxt - calParams.COPx;
  const y = pyt - calParams.COPy;

  const V = calParams.V;
  const S = calParams.S;
  const D = calParams.D;
  const a0 = (calParams.a0 * Math.PI) / 180.0;
  const E = (calParams.E * Math.PI) / 180.0;
  const eps = (calParams.eps * Math.PI) / 180.0;

  const r = Math.sqrt(x * x + y * y);
  const u = V * r + S * (Math.exp(D * r) - 1);
  const b = a0 - E + Math.atan2(x, y);

  let angle = b;
  let z = u;
  if (eps != 0.0) {
    z = Math.acos(
      Math.cos(u) * Math.cos(eps) - Math.sin(u) * Math.sin(eps) * Math.cos(b),
    );
    let sinAngle = (Math.sin(b) * Math.sin(u)) / Math.sin(z);
    let cosAngle =
      (Math.cos(u) - Math.cos(eps) * Math.cos(z)) /
      (Math.sin(eps) * Math.sin(z));
    angle = Math.atan2(sinAngle, cosAngle);
  }

  let obj = {};

  obj.elev = Math.PI / 2 - z;
  obj.azim = angle + E; // Measured from cardinal SOUTH.

  return obj;
}

async function getCalibrationEvents() {
  try {
    let data = await post("calEvents", {});
    calibrationEvents.val = data;
  } catch (error) {
    alert(error);
  }
}

function nelderMead2D(f, initial, step = 1, tol = 1e-6, maxIter = 200) {
  // Create simplex: three points for 2D
  let simplex = [
    [initial[0], initial[1]],
    [initial[0] + step, initial[1]],
    [initial[0], initial[1] + step],
  ];
  let values = simplex.map((p) => f(p));

  for (let iter = 0; iter < maxIter; iter++) {
    // Sort simplex by function value
    let idx = [0, 1, 2].sort((a, b) => values[a] - values[b]);
    simplex = idx.map((i) => simplex[i]);
    values = idx.map((i) => values[i]);

    // Centroid of best two
    let centroid = [
      (simplex[0][0] + simplex[1][0]) / 2,
      (simplex[0][1] + simplex[1][1]) / 2,
    ];

    // Reflection
    let reflected = [
      centroid[0] + (centroid[0] - simplex[2][0]),
      centroid[1] + (centroid[1] - simplex[2][1]),
    ];
    let reflectedValue = f(reflected);

    if (reflectedValue < values[1]) {
      // Expansion
      let expanded = [
        centroid[0] + 2 * (centroid[0] - simplex[2][0]),
        centroid[1] + 2 * (centroid[1] - simplex[2][1]),
      ];
      let expandedValue = f(expanded);
      if (expandedValue < reflectedValue) {
        simplex[2] = expanded;
        values[2] = expandedValue;
      } else {
        simplex[2] = reflected;
        values[2] = reflectedValue;
      }
    } else if (reflectedValue < values[2]) {
      simplex[2] = reflected;
      values[2] = reflectedValue;
    } else {
      // Contraction
      let contracted = [
        centroid[0] + 0.5 * (simplex[2][0] - centroid[0]),
        centroid[1] + 0.5 * (simplex[2][1] - centroid[1]),
      ];
      let contractedValue = f(contracted);
      if (contractedValue < values[2]) {
        simplex[2] = contracted;
        values[2] = contractedValue;
      } else {
        // Shrink
        simplex[1] = [
          simplex[0][0] + 0.5 * (simplex[1][0] - simplex[0][0]),
          simplex[0][1] + 0.5 * (simplex[1][1] - simplex[0][1]),
        ];
        simplex[2] = [
          simplex[0][0] + 0.5 * (simplex[2][0] - simplex[0][0]),
          simplex[0][1] + 0.5 * (simplex[2][1] - simplex[0][1]),
        ];
        values[1] = f(simplex[1]);
        values[2] = f(simplex[2]);
      }
    }

    // Convergence criteria
    let diff = Math.max(
      Math.abs(values[0] - values[1]),
      Math.abs(values[0] - values[2]),
    );
    if (diff < tol) break;
  }

  return { x: simplex[0], fx: values[0] };
}

// // Convert azimuth and elevation to degrees;
// let RadToDeg = function( obj ) {
//   // Subtract PI tp express azimuth conventionally, i.e., measured from cardinal North
//   let azim = obj.azim - Math.PI;

//   // Convert radians to degrees
//   let elev = obj.elev * 180.0 / Math.PI;
//   azim = azim * 180.0 / Math.PI;

//   // Compute azimuth on the interval [0, 360)
//   while (azim < 0.0) {
//     azim = azim + 360.0;
//   }

//   while (azim >= 360.0) {
//     azim = azim - 360.0;
//   }

//   let newObj = {}
//   newObj.name = obj.name;
//   newObj.azim = azim;
//   newObj.elev = elev;

//   return newObj;
// }

// Convert azimuth and elevation to radians
function DegToRad(obj) {
  // Add 180 tp express azimuth measured from cardinal Soutn
  let azim = obj.azim + 180.0;

  // Convert degrees to radians
  let elev = (obj.elev * Math.PI) / 180.0;
  azim = (azim * Math.PI) / 180.0;

  let newObj = {};
  newObj.name = obj.name;
  newObj.azim = azim;
  newObj.elev = elev;

  return newObj;
}

function Cartesian(obj) {
  let r = Math.PI / 2.0 - obj.elev;
  let px = r * Math.cos(obj.azim);
  let py = r * Math.sin(obj.azim);

  return [px, py];
}

function AzElToPixel(obj) {
  // Convert azimuth and elevation to pixel coordinates
  // Azimuth in radians measured from cardinal SOUTH

  let cart0 = Cartesian(obj);

  let f = function (v) {
    let azel = PixelToAzEl(v[0], v[1]);
    let cart = Cartesian(azel);

    let dx = cart[0] - cart0[0];
    let dy = cart[1] - cart0[1];

    return dx * dx + dy * dy;
  };

  let result = nelderMead2D(f, [900, 500], 100);
  let vec = result.x;

  let newObj = { ...obj };

  newObj.px = Math.round(vec[0]);
  newObj.py = Math.round(vec[1]);

  return newObj;
}

function Transform(azel) {
  let temp = { ...azel };
  temp = DegToRad(temp);
  temp = AzElToPixel(temp);

  temp.azim = azel.azim;
  temp.elev = azel.elev;

  return temp;
}

function utcToLocal(utc) {
  const date = new Date();
  date.setUTCHours(utc.h, utc.m);
  let shours = String(date.getHours()).padStart(2, "0");
  let sminutes = String(date.getMinutes()).padStart(2, "0");
  let t = shours + ":" + sminutes;

  return t;
}

const header = div({ class: "header-label" }, h3(" Pi Sentinel "));

async function requestControls() {
  try {
    const result = await post("get_state");
    if (Object.keys(result).length === 0) {
      alert("Get State failed");
    } else {
      populateForm1(result);
    }
  } catch (error) {
    alert(error);
  }
}

async function changeTable(value) {
  tableSelection = value;

  try {
    let data = await post(tableSelection, eventArray.rawVal);
    eventArray.val = data;
  } catch (error) {
    alert(error);
  }
}

function selectEvents() {
  selectedMode.val = "events";
  changeTable(tableSelection);
}

function selectControls() {
  selectedMode.val = "controls";
  requestControls();
}

function selectCals() {
  selectedMode.val = "cals";
  getCalibrationParameters();
  getCalibrationEvents();
  getCalibrationSamples();
}

const modeButtons = div(
  { class: "button-group" },
  button(
    {
      class: () => (selectedMode.val === "events" ? "active" : ""),
      onclick: selectEvents,
    },
    "Events",
  ),
  button(
    {
      class: () => (selectedMode.val === "controls" ? "active" : ""),
      onclick: selectControls,
    },
    "Controls",
  ),
  button(
    {
      class: () => (selectedMode.val === "cals" ? "active" : ""),
      onclick: selectCals,
    },
    "Cals",
  ),
);

const actionSelector = () => {
  const actionOpen = van.state(false);

  const toggle = () => (actionOpen.val = !actionOpen.val);
  const close = () => (actionOpen.val = false);

  const toggleStartStop = async () => {
    close();
    try {
      // console.log("toggleStartStop");
      const result = await post("toggle_camera");
      if (result.response != "OK") {
        alert(`Toggle failed: ${result.response}`);
      }
    } catch (error) {
      alert(error);
    }
  };

  const forceTrigger = async () => {
    close();
    try {
      const result = await post("force_trigger");
      if (result.response != "OK") {
        alert("Force Trigger failed");
      } else {
        setTimeout(requestControls, 5000);
        setTimeout(changeTable, 6000, tableSelection);
      }
    } catch (error) {
      alert(error);
    }
  };

  const toggleStars = () => {
    close();
    showStars.val = !showStars.rawVal;
  };

  const makeComposite = async () => {
    close();

    const dir = eventId.rawVal.dir;
    const id = eventId.rawVal.id;
    if (id == "") return;

    const hpath = `${dir}/${id}.h264`;

    let obj = {};
    obj.path = hpath;

    try {
      const result = await post("compose", obj);
      if (result.response !== "OK") {
        alert("Cannot analyze while running");
      } else {
        setTimeout(waitForCompletion, 3000);
      }
    } catch (error) {
      alert(error);
    }
  };

  const analyze = async () => {
    close();

    const dir = eventId.rawVal.dir;
    const id = eventId.rawVal.id;

    if (id == "") return;

    const hpath = `${dir}/${id}.h264`;

    let obj = {};
    obj.path = hpath;

    try {
      const result = await post("analyze", obj);
      if (result.response != "OK") {
        alert("Cannot analyze while running");
      } else {
        setTimeout(waitForCompletion, 3000);
      }
    } catch (error) {
      alert(error);
    }
  };

  const playback = () => {
    close();
    playbackDialog.showModal();
  };

  const average = () => {
    close();
    averageDialog.showModal();
  };

  const retest = () => {
    close();
    retestDialog.showModal();
  };

  const downloadJpg = () => {
    close();
    if (!goodImage) return;

    const link = a();
    link.href = jpgPath.rawVal;
    link.download = eventId.rawVal.id + ".jpg";
    van.add(document.body, link);
    link.click();
    document.body.removeChild(link);
  };

  const downloadCsv = () => {
    close();
    if (!goodImage) return;

    const link = a();
    link.href = mp4Path.rawVal;
    link.download = eventId.rawVal.id + ".csv";
    van.add(document.body, link);
    link.click();
    document.body.removeChild(link);
  };

  const downloadMp4 = () => {
    close();
    if (!goodVideo) return;

    const link = a();
    link.href = mp4Path.rawVal;
    link.download = eventId.rawVal.id + ".mp4";
    van.add(document.body, link);
    link.click();
    document.body.removeChild(link);
  };

  const recalcStartStopTimes = async () => {
    close();
    const minutes = await promptDialog("Minutes of Twilight", "30");
    if (minutes === null) return;

    let body = {
      twilight: Number(minutes),
      lat: calibrationParameters.rawVal.cameraLatitude,
      lon: calibrationParameters.rawVal.cameraLongitude,
    };

    try {
      const data = await post("recalc_start_stop_times", body);
      if (data.response != "OK") {
        alert("Recalc Start Stop Times failed");
        return;
      }

      let startTime = utcToLocal(data.startUTC);
      let stopTime = utcToLocal(data.stopUTC);

      let startElement = document.querySelector("#startTime");
      let stopElement = document.querySelector("#stopTime");

      startElement.value = startTime;
      stopElement.value = stopTime;
    } catch (error) {
      alert(error);
    }
  };

  const emptyTrash = async () => {
    close();
    try {
      const data = await post("empty_trash");
      if (data.response != "OK") {
        alert("Empty Trash failed");
        return;
      }
    } catch (error) {
      alert(error);
    }
  };

  const sectionEl = section(
    { class: "dropdown" },
    button({ onclick: toggle }, "Cmds ▾"),
    // menu
    div(
      { class: () => `dropdown-menu ${actionOpen.val ? "show" : ""}` },
      button({ onclick: toggleStartStop }, "Toggle Start/Stop"),
      button({ onclick: forceTrigger }, "Force Trigger"),
      button({ onclick: makeComposite }, "Make Composite"),
      button({ onclick: analyze }, "Analyze"),
      button({ onclick: average }, "Stargaze"),
      button({ onclick: playback }, "Playback"),
      // button({ onclick: retest }, "Retest Archive Data"),
      button({ onclick: downloadJpg }, "Get Image"),
      button({ onclick: downloadMp4 }, "Get Video"),
      button({ onclick: downloadCsv }, "Get CSV"),
      button({ onclick: recalcStartStopTimes }, "Recalc Times"),
      button({ onclick: emptyTrash }, "Empty Trash"),
    ),
  );

  document.addEventListener("click", (e) => {
    if (actionOpen.val && !sectionEl.contains(e.target)) {
      close();
    }
  });

  return sectionEl;
};

const topNav =
  ({ class: "panel-section" },
  div({ class: "topnav" }, actionSelector, modeButtons));

async function submitForm1(e) {
  e.preventDefault();
  const fd = new FormData(e.target);
  const obj = Object.fromEntries(fd.entries());
  let hours = 0;
  let minutes = 0;
  let date = new Date();
  let newObj = {};
  for (const [key, value] of Object.entries(obj)) {
    switch (key) {
      case "devName":
        newObj[key] = value == "" ? "/dev/video2" : value;
        break;
      case "archivePath":
        newObj[key] = value == "" ? "none" : value;
        break;
      case "startTime":
        hours = +value.substring(0, 2);
        minutes = +value.substring(3, 5);
        date.setHours(hours);
        date.setMinutes(minutes);
        newObj.startUTC = {
          h: date.getUTCHours(),
          m: date.getUTCMinutes(),
        };
        newObj.startTime = { h: hours, m: minutes };
        break;
      case "stopTime":
        hours = +value.substring(0, 2);
        minutes = +value.substring(3, 5);
        date.setHours(hours);
        date.setMinutes(minutes);
        newObj.stopUTC = {
          h: date.getUTCHours(),
          m: date.getUTCMinutes(),
        };
        newObj.stopTime = { h: hours, m: minutes };
        break;
      default:
        newObj[key] = +value;
    }
  }

  try {
    const result1 = await post("set_state", newObj);
    if (result1.response != "OK") {
      alert("Set State failed");
    }
    requestControls();
  } catch (error) {
    alert(error);
  }
}

function populateForm1(jsonData) {
  const form = document.getElementById("form1");
  if (!form) {
    console.error("Form not found!");
    return;
  }

  let date = new Date();
  let nums = { numNew: 0, numSaved: 0, numTrashed: 0 };
  let times = { startUTC: "startTime", stopUTC: "stopTime" };
  let nextFormElement;

  for (const key in jsonData) {
    if (Object.hasOwn(jsonData, key)) {
      const value = jsonData[key];
      const formElement = form.elements[key];

      if (formElement) {
        formElement.value = value;
      } else {
        switch (key) {
          case "startUTC":
          case "stopUTC":
            let t = utcToLocal(value);
            nextFormElement = form.elements[times[key]];
            nextFormElement.value = t;
            break;
          case "numNew":
          case "numSaved":
          case "numTrashed":
            nums[key] = value;
        }
      }

      nextFormElement = form.elements["nst"];
      if (nextFormElement) {
        nextFormElement.value = `${nums.numNew},${nums.numSaved},${nums.numTrashed}`;
      }
    }
  }
}

const form1 = section(
  { class: "panel-section", style: "display: none;" },
  form(
    { id: "form1", class: "form-grid", onsubmit: submitForm1 },
    label("Running:"),
    input({
      type: "text",
      name: "running",
      class: "readonly",
      readonly: "",
      value: "No",
    }),

    label("New, Saved, Trash:"),
    input({
      type: "text",
      name: "nst",
      class: "readonly",
      readonly: "",
      value: "0,0,0",
    }),

    label("Frame Rate:"),
    input({
      type: "text",
      name: "frameRate",
      class: "readonly",
      readonly: "",
      value: "30.0",
    }),

    label("Zenith Amplitude:"),
    input({
      type: "text",
      name: "zenithAmplitude",
      class: "readonly",
      readonly: "",
      value: "0.0",
    }),

    label("GPS Latitude:"),
    input({
      type: "text",
      name: "gpsLatitude",
      class: "readonly",
      readonly: "",
      value: "0.0",
    }),

    label("GPS Longitude:"),
    input({
      type: "text",
      name: "gpsLongitude",
      class: "readonly",
      readonly: "",
      value: "0.0",
    }),

    label("GPS Time Offset:"),
    input({
      type: "text",
      name: "gpsTimeOffset",
      class: "readonly",
      readonly: "",
      value: "0.0",
    }),

    label({ for: "startTime" }, "Auto Start Time:"),
    input({
      id: "startTime",
      type: "time",
      name: "startTime",
      value: "18:30",
    }),

    label({ for: "stopTime" }, "Auto Stop Time:"),
    input({
      id: "stopTime",
      type: "time",
      name: "stopTime",
      value: "06:30",
    }),

    label({ for: "noiseThreshold" }, "Noise Threshold:"),
    input({ type: "number", name: "noiseThreshold", value: "50" }),

    label({ for: "sumTrigger" }, "Trigger Threshold:"),
    input({ type: "number", name: "sumThreshold", value: "50" }),

    label({ for: "eventsPerHour" }, "Max Events/Hour:"),
    input({ type: "number", name: "eventsPerHour", value: "10" }),

    label({ for: "devName" }, "Camera Device:"),
    input({ type: "text", name: "devName", value: "/dev/video2" }),

    label({ for: "archivePath" }, "Archive Path:"),
    input({ type: "text", name: "archivePath", value: "/home/pi/archive" }),

    span(),
    button({ type: "submit" }, "Submit"),
  ),
);

async function submitForm2(e) {
  e.preventDefault();
  const fd = new FormData(e.target);
  const obj = Object.fromEntries(fd.entries());
  for (const [key, value] of Object.entries(obj)) {
    obj[key] = +value;
  }

  calibrationParameters.val = obj;
  try {
    await post("save_calibration", obj);
  } catch (error) {
    alert(error);
  }
}

async function getCalibrationParameters() {
  try {
    const data = await get("get_calibration");
    if (data && Object.keys(data).length !== 0) {
      calibrationParameters.val = data;
      populateForm2(data);
    }
  } catch (error) {
    alert(error);
  }
}

async function getCalibrationSamples() {
  try {
    const data = await get("get_sky_objects");
    skySamples.val = data;
  } catch (error) {
    alert(error);
  }
}

function populateForm2(jsonData) {
  const form = document.getElementById("form2");
  if (!form) {
    console.error("Form not found!");
    return;
  }

  for (const key in jsonData) {
    if (Object.hasOwn(jsonData, key)) {
      const value = jsonData[key];
      const formElement = form.elements[key];

      if (formElement) {
        // Handle different input types (basic text, number, etc.)
        if (formElement.type === "text" || formElement.type === "number") {
          if (key == "a0" && value < 0.0) {
            formElement.value = value + 360.0;
          } else {
            formElement.value = value;
          }
        }
      }
    }
  }
}

const form2 = section(
  { class: "panel-section", style: "display: none;" },
  form(
    { id: "form2", class: "form-grid", onsubmit: submitForm2 },

    label({ for: "f2-lat" }, "Latitude:"),
    input({
      type: "text",
      id: "f2-lat",
      name: "cameraLatitude",
      value: "0.0",
    }),

    label({ for: "f2-lon" }, "Longitude:"),
    input({
      type: "text",
      id: "f2-lon",
      name: "cameraLongitude",
      value: "0.0",
    }),

    label({ for: "f2-el" }, "Elevation:"),
    input({
      type: "text",
      id: "f2-el",
      name: "cameraElevation",
      value: "0.0",
    }),

    label({ for: "f2-COPx" }, "COPx:"),
    input({ type: "text", id: "f2-COPx", name: "COPx", value: "960.0" }),

    label({ for: "f2-COPy" }, "COPy:"),
    input({ type: "text", id: "f2-COPy", name: "COPy", value: "540.0" }),

    label({ for: "f2-a0" }, "a0:"),
    input({ type: "text", id: "f2-a0", name: "a0", value: "0.0" }),

    label({ for: "f2-V" }, "V:"),
    input({ type: "text", id: "f2-V", name: "V", value: "0.02" }),

    label({ for: "f2-S" }, "S:"),
    input({ type: "text", id: "f2-S", name: "S", value: "0.0" }),

    label({ for: "f2-D" }, "D:"),
    input({ type: "text", id: "f2-D", name: "D", value: "0.0" }),

    label({ for: "f2-E" }, "E:"),
    input({ type: "text", id: "f2-E", name: "E", value: "0.0" }),

    label({ for: "f2-eps" }, "eps:"),
    input({ type: "text", id: "f2-eps", name: "eps", value: "0.0" }),

    label({ for: "f2-alpha" }, "alpha:"),
    input({ type: "text", id: "f2-alpha", name: "alpha", value: "0.0" }),

    label({ for: "f2-flat" }, "flat:"),
    input({ type: "text", id: "f2-flat", name: "flat", value: "0.0" }),

    div(),
    button({ type: "submit" }, "Submit"),
  ),
);

const videoPlayer1 = video({
  class: "responsive",
  preload: "metadata",
  playsinline: true,
  muted: true,
  controls: true,
  width: "1920",
  height: "1080",
  src: "",
  oncanplay: () => {
    videoPlayer1.classList.remove("invisible");
    videoPlayer2.classList.add("invisible");
  },
});

const videoPlayer2 = video({
  class: "responsive",
  preload: "metadata",
  playsinline: true,
  muted: true,
  controls: true,
  width: "1920",
  height: "1080",
  src: "",
  oncanplay: () => {
    videoPlayer2.classList.remove("invisible");
    videoPlayer1.classList.add("invisible");
  },
});

async function handleImageClick(e) {
  const rect = imagePlayer.getBoundingClientRect();

  // 1) position within rendered image
  const x = e.clientX - rect.left;
  const y = e.clientY - rect.top;

  // 2) normalize
  const xRatio = x / rect.width;
  const yRatio = y / rect.height;

  // 3) scale to original (natural) size
  const origX = xRatio * 1920;
  const origY = yRatio * 1080;

  let nearest = null;
  let distance = 0;

  for (const star of starObjects.rawVal) {
    let dx = star.px - origX;
    let dy = star.py - origY;

    let dist = Math.sqrt(dx * dx + dy * dy);
    if (!nearest || dist < distance) {
      nearest = star;
      distance = dist;
    }
  }

  if (nearest) {
    if (
      await confirmDialog(
        `Match ${nearest.name} ${Math.round(distance)} pixels away`,
      )
    ) {
      nearest.px = origX;
      nearest.py = origY;
      nearest.id = eventId.rawVal.id;
      skySamples.val = skySamples.rawVal.concat([nearest]);
      saveSkySamples();
    }
  }
}

const imagePlayer = img({
  class: "responsive meteorImage",
  src: "",
  onclick: handleImageClick,
});

const starPlayer = img({ class: starClasses, src: svgStarString });

const vcontent = div(
  { class: "content" },
  videoPlayer1,
  videoPlayer2,
  imagePlayer,
  starPlayer,
);

const tableSelect = section(
  {
    id: "tableSelect",
    class: "radio-group",
    style: "display: none;",
    onchange: (e) => changeTable(e.target.value),
  },
  input({
    type: "radio",
    id: "new",
    name: "folder",
    value: "new",
    checked: "true",
  }),
  label({ for: "new" }, "New"),

  input({ type: "radio", id: "saved", name: "folder", value: "saved" }),
  label({ for: "saved" }, "Saved"),

  input({ type: "radio", id: "trash", name: "folder", value: "trash" }),
  label({ for: "trash" }, "Trash"),
);

function tableClicked(event) {
  const cell = event.target.closest("td");
  if (!cell) return;

  const row = cell.parentElement;
  const rowIndex = row.rowIndex;
  const columnIndex = cell.cellIndex;
  const tableBody = row.parentElement;

  if (columnIndex == 0) {
    const allRows = tableBody.querySelectorAll("tr");
    allRows.forEach((r) => r.classList.remove("selected"));

    row.classList.add("selected");

    const eventObj = eventArray.rawVal[rowIndex - 1];
    let base = eventObj.event;
    goodVideo = eventObj.m;
    goodImage = eventObj.j;
    if (goodImage && event.shiftKey) {
      base += "m";
      goodVideo = false;
    }
    eventId.val = { dir: tableSelection, id: base };
  } else if (columnIndex == 1) {
    const fsm = { new: "trash", trash: "saved", saved: "new" };
    const next = fsm[cell.textContent];
    cell.textContent = next;
    cell.classList = next;
    eventArray.rawVal[rowIndex - 1].to = next;
  }
}

async function calibrate() {
  const body = {
    sky_object_list: skySamples.rawVal,
    calibration_state: calibrationParameters.rawVal,
  };

  try {
    const result = await post("do_calibration", body);
    if (result.response != "OK") {
      alert(result.response);
      return;
    }

    calibrationParameters.val = result.calibration_state;
    populateForm2(calibrationParameters.rawVal);
  } catch (error) {
    alert(error);
  }
}

async function saveSkySamples() {
  const body = { sky_object_list: skySamples.rawVal };
  try {
    const result = await post("save_sky_objects", body);
    if (result.response != "OK") {
      alert(result.response);
      return;
    }
  } catch (error) {
    alert(error);
  }
}

const calModeSelect = select(
  {
    class: "calModeSelect",
    onchange: () => (calMode.val = calModeSelect.value),
  },
  option({ value: "params" }, "Params"),
  option({ value: "images" }, "Images"),
  option({ value: "samples" }, "Samples"),
);

const calButtons = section(
  { class: "calButtons" },
  calModeSelect,
  button({ type: "button", onclick: () => (skySamples.val = []) }, "Clear All"),
  button({ type: "button", onclick: calibrate }, "Calibrate"),
);

const eventTable = section(
  { class: "table panel-section", style: "display: none;" },
  table(
    { onclick: tableClicked },
    colgroup(col({ style: "width: 200px;" }), col({ style: "width: auto" })),
    thead(tr(th("Events"), th("Move To"))),
    () =>
      tbody(
        eventArray.val.map((row) =>
          tr(td(row.event), td({ class: row.from }, row.from)),
        ),
      ),
  ),
);

function calTableClicked(event) {
  const cell = event.target.closest("td");
  if (!cell) return;

  const row = cell.parentElement;
  const rowIndex = row.rowIndex;
  const columnIndex = cell.cellIndex;
  const tableBody = row.parentElement;

  if (columnIndex == 0) {
    const allRows = tableBody.querySelectorAll("tr");
    allRows.forEach((r) => r.classList.remove("selected"));

    row.classList.add("selected");

    const event = calibrationEvents.rawVal[rowIndex - 1];
    goodVideo = false;
    goodImage = true;
    eventId.val = { dir: "calibration", id: event };
  }
}

const starTable = div(
  { class: "table", style: "display: none" },
  table(
    { onclick: calTableClicked },
    colgroup(col({ style: "width: 220px" })),
    thead(tr(th("Cal Images"))),
    () => tbody(calibrationEvents.val.map((row) => tr(td(row)))),
  ),
);

async function sampleTableClicked(e) {
  const cell = e.target;
  const row = cell.parentElement;
  const rowIndex = row.rowIndex;

  if (await confirmDialog(`Remove ${skySamples.val[rowIndex - 1].name}?`))
    skySamples.val = skySamples.rawVal.filter(
      (row) => row.id != skySamples.val[rowIndex - 1].id,
    );
}

const skyObjectTable = section(
  { class: "table panel-selection", style: "display: none" },
  table(
    { onclick: sampleTableClicked },
    colgroup(col({ style: "width: 200px;" }), col({ style: "width: auto" })),
    thead(tr(th("Cal Samples"), th("Stars, etc."))),
    () => tbody(skySamples.val.map((row) => tr(td(row.id), td(row.name)))),
  ),
);

const footer = div(
  { class: "footer" },
  () => eventTime.val,
  label(
    {
      style:
        "margin-left: 1rem; color: #484f58; user-select: none; cursor: pointer;",
    },
    input({
      type: "checkbox",
      style: "margin-right: 0.4rem; cursor: pointer;",
      checked: () => showStars.val,
      onchange: (e) => {
        showStars.val = e.target.checked;
      },
    }),
    "Show Stars",
  ),
);

const left = section(
  { class: "left-panel" },
  header,
  topNav,
  form1,
  tableSelect,
  eventTable,
  calButtons,
  form2,
  skyObjectTable,
  starTable,
);

const right = div({ class: "right-panel" }, vcontent, footer);

const container = main({ class: "app" }, left, right);

function svgRender(st) {
  return st.name.length > 1
    ? `<circle cx="${st.px}" cy="${st.py}" r="10" fill="none" stroke="red" stroke-width="1" />`
    : `<circle cx="${st.px}" cy="${st.py}" r="5" fill="red" stroke="red" />`;
}

function svgTextRender(st) {
  return `<text x="${st.px + 10}" y="${st.py - 10}" fill="red" font-family="Arial" font-size="24">${st.name}</text>`;
}

function init() {
  selectEvents();
  getCalibrationParameters();
  subscribe();
}

async function submitPlayback(e) {
  // console.log("submitPlayback", e);
  e.preventDefault();
  playbackDialog.close();
  const fd = new FormData(e.target);
  const obj = Object.fromEntries(fd.entries());

  const date = new Date(obj.date + "T" + obj.time);
  obj.timestamp = date.getTime();

  obj.duration = +obj.duration;

  try {
    const result = await post("playback", obj);
    if (result.response != "OK") {
      alert(result.response);
    }
  } catch (error) {
    alert(error);
  }
}

const playbackDialog = dialog(
  h2("Event Playback"),
  form(
    { method: "dialog", class: "form-grid", onsubmit: submitPlayback },
    label({ for: "date" }, "Date: "),
    input({ type: "date", name: "date", value: formattedDate }),
    label({ for: "time" }, "Time: "),
    input({
      type: "time",
      step: 1,
      name: "time",
      value: formattedTime,
    }),
    label({ for: "duration" }, "Duration (sec): "),
    input({ name: "duration", type: "number", value: 10 }),
    button({ type: "submit" }, "Submit"),
    button({ type: "button", onclick: () => playbackDialog.close() }, "Cancel"),
  ),
);

async function submitRetest(e) {
  // console.log("submitRetest", e);
  e.preventDefault();
  playbackDialog.close();
  const fd = new FormData(e.target);
  const obj = Object.fromEntries(fd.entries());

  const date = new Date(obj.date + "T" + obj.time + ":00");
  obj.timestamp = date.getTime();

  obj.duration = +obj.duration;

  try {
    const result = await post("retest", obj);
    if (result.response != "OK") {
      alert(result.response);
    }
  } catch (error) {
    alert(error);
  }
}

const retestDialog = dialog(
  h2("Retest Archive Data"),
  form(
    { method: "dialog", class: "form-grid", onsubmit: submitRetest },
    label({ for: "date" }, "Date: "),
    input({ type: "date", name: "date", value: formattedDate }),
    label({ for: "time" }, "Time: "),
    input({
      type: "time",
      step: 1,
      name: "time",
      value: formattedHourMinute,
    }),
    label({ for: "duration" }, "Duration (min): "),
    input({ name: "duration", type: "number", value: 5 }),
    button({ type: "submit" }, "Submit"),
    button({ type: "button", onclick: () => retestDialog.close() }, "Cancel"),
  ),
);

async function submitAverage(e) {
  // console.log("submitAverage", e);
  e.preventDefault();
  averageDialog.close();
  const fd = new FormData(e.target);
  const obj = Object.fromEntries(fd.entries());
  const date = new Date(obj.date + "T" + obj.time + ":00");
  obj.timestamp = date.getTime();

  try {
    const result = await post("average", obj);
    if (result.response != "OK") {
      alert(result.response);
    } else {
      waitForCompletion();
    }
  } catch (error) {
    alert(error);
  }
}

const averageDialog = dialog(
  h2("Stargaze"),
  form(
    { method: "dialog", class: "form-grid", onsubmit: submitAverage },
    label({ for: "date" }, "Date: "),
    input({ type: "date", name: "date", value: formattedDate }),
    label({ for: "time" }, "Time: "),
    input({
      type: "time",
      name: "time",
      value: formattedHourMinute,
    }),
    button({ type: "submit" }, "Submit"),
    button({ type: "button", onclick: () => averageDialog.close() }, "Cancel"),
  ),
);

// CONFIRM DIALOG
const confirmDlg = dialog(
  { class: "modal" },

  p(() => confirmText.val),

  div(
    { class: "modal-buttons" },
    button({ id: "confirm-cancel" }, "Cancel"),
    button({ id: "confirm-ok" }, "OK"),
  ),
);

function confirmDialog(message) {
  return new Promise((resolve) => {
    confirmText.val = message;
    confirmDlg.showModal();

    const cancelBtn = confirmDlg.querySelector("#confirm-cancel");
    const okBtn = confirmDlg.querySelector("#confirm-ok");

    const cleanup = () => {
      cancelBtn.onclick = null;
      okBtn.onclick = null;
    };

    cancelBtn.onclick = () => {
      cleanup();
      confirmDlg.close();
      resolve(false);
    };

    okBtn.onclick = () => {
      cleanup();
      confirmDlg.close();
      resolve(true);
    };
  });
}

const promptDlg = dialog(
  { class: "modal prompt-dialog" },

  p(() => promptText.val),

  input({
    type: "text",
    value: promptValue,
    oninput: (e) => (promptValue.val = e.target.value),
  }),

  div(
    { class: "modal-buttons" },
    button({ id: "prompt-cancel" }, "Cancel"),
    button({ id: "prompt-ok" }, "OK"),
  ),
);

function promptDialog(message, defaultValue = "") {
  return new Promise((resolve) => {
    promptText.val = message;
    promptValue.val = defaultValue;

    promptDlg.showModal();

    const cancelBtn = promptDlg.querySelector("#prompt-cancel");
    const okBtn = promptDlg.querySelector("#prompt-ok");
    const inputField = promptDlg.querySelector("input");

    inputField.focus();
    inputField.select();

    const cleanup = () => {
      cancelBtn.onclick = null;
      okBtn.onclick = null;
    };

    cancelBtn.onclick = () => {
      cleanup();
      promptDlg.close();
      resolve(null);
    };

    okBtn.onclick = () => {
      cleanup();
      promptDlg.close();
      resolve(promptValue.val);
    };
  });
}

document.addEventListener("keydown", (e) => {
  // Only handle arrow keys when the event table is visible
  if (eventTable.style.display === "none") return;
  if (!["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"].includes(e.key))
    return;

  // Don't intercept when focus is inside an input, textarea, or an open dialog
  const active = document.activeElement;
  if (active && (active.tagName === "INPUT" || active.tagName === "TEXTAREA"))
    return;
  if (active && active.closest("dialog[open]")) return;

  const tableBody = eventTable.querySelector("tbody");
  if (!tableBody) return;
  const allRows = Array.from(tableBody.querySelectorAll("tr"));
  if (allRows.length === 0) return;

  const selectedRow = tableBody.querySelector("tr.selected");

  if (e.key === "ArrowDown" || e.key === "ArrowUp") {
    e.preventDefault();
    let targetRow;
    if (!selectedRow) {
      targetRow =
        e.key === "ArrowDown" ? allRows[allRows.length - 1] : allRows[0];
    } else {
      const currentIndex = allRows.indexOf(selectedRow);
      targetRow =
        e.key === "ArrowDown"
          ? allRows[Math.min(currentIndex + 1, allRows.length - 1)]
          : allRows[Math.max(currentIndex - 1, 0)];
    }
    allRows.forEach((r) => r.classList.remove("selected"));
    targetRow.classList.add("selected");
    targetRow.scrollIntoView({ block: "nearest" });
    const eventObj = eventArray.rawVal[targetRow.rowIndex - 1];
    goodVideo = eventObj.m;
    goodImage = eventObj.j;
    eventId.val = { dir: tableSelection, id: eventObj.event };
  } else if (e.key === "ArrowRight" && selectedRow) {
    e.preventDefault();
    const cell = selectedRow.cells[1];
    const fsm = { new: "trash", trash: "saved", saved: "new" };
    const next = fsm[cell.textContent];
    cell.textContent = next;
    cell.className = next;
    eventArray.rawVal[selectedRow.rowIndex - 1].to = next;
  } else if (e.key === "ArrowLeft" && selectedRow) {
    e.preventDefault();
    const eventObj = eventArray.rawVal[selectedRow.rowIndex - 1];
    goodVideo = eventObj.m;
    goodImage = eventObj.j;
    eventId.val = { dir: tableSelection, id: eventObj.event };
  }
});

van.add(document.body, container);
van.add(document.body, playbackDialog);
van.add(document.body, averageDialog);
van.add(document.body, retestDialog);
van.add(document.body, confirmDlg);
van.add(document.body, promptDlg);

init();

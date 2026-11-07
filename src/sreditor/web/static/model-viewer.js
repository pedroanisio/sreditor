// The 3D model viewer (rule project.external-media): draws a GLB or FBX copy in its frame with
// the vendored three.js. It draws only when something changes (a drag, a zoom, a resize), so an
// idle page costs nothing and nothing moves on its own (rule ui.motion). workspace.js imports
// this module only on pages that show a viewer and calls mount() once for each.
import {
  Box3,
  DirectionalLight,
  HemisphereLight,
  PerspectiveCamera,
  PMREMGenerator,
  Scene,
  Vector3,
  WebGLRenderer,
} from "./vendor/three/build/three.module.js";
import { OrbitControls } from "./vendor/three/examples/jsm/controls/OrbitControls.js";
import { RoomEnvironment } from "./vendor/three/examples/jsm/environments/RoomEnvironment.js";

var FIELD_OF_VIEW = 40;
// How far past the model's bounding sphere the camera starts, so it fits with a margin.
var FIT_MARGIN = 1.3;
var NEAR_RATIO = 0.01;
var FAR_RATIO = 100;
var MAX_PIXEL_RATIO = 2;
var SKY_LIGHT = 0xffffff;
var GROUND_LIGHT = 0x444444;
var KEY_LIGHT_POSITION = [1, 2, 1.5];
var LOADERS = {
  glb: function () {
    return import("./vendor/three/examples/jsm/loaders/GLTFLoader.js").then(function (module) {
      return { loader: new module.GLTFLoader(), root: function (found) { return found.scene; } };
    });
  },
  fbx: function () {
    return import("./vendor/three/examples/jsm/loaders/FBXLoader.js").then(function (module) {
      return { loader: new module.FBXLoader(), root: function (found) { return found; } };
    });
  },
};

function say(figure, state) {
  figure.classList.remove("is-loading", "is-ready", "is-error");
  figure.classList.add("is-" + state);
  var status = figure.querySelector("[role=status]");
  status.textContent = status.getAttribute("data-" + state);
  figure.querySelector("[data-model-reset]").disabled = state !== "ready";
}

// A neutral studio for metal to reflect (glTF materials are metallic unless they say otherwise,
// and metal with nothing to reflect looks black), plus a key light for shape.
function lights(scene, renderer) {
  var studio = new PMREMGenerator(renderer);
  scene.environment = studio.fromScene(new RoomEnvironment()).texture;
  studio.dispose();
  scene.add(new HemisphereLight(SKY_LIGHT, GROUND_LIGHT, 1));
  var key = new DirectionalLight(SKY_LIGHT, 2);
  key.position.fromArray(KEY_LIGHT_POSITION);
  scene.add(key);
}

// Put the model in the middle of the view, whatever its size or units.
function frame(model, camera, controls) {
  var bounds = new Box3().setFromObject(model);
  var radius = bounds.getBoundingSphere({ center: new Vector3(), radius: 0 }).radius || 1;
  var center = bounds.getCenter(new Vector3());
  var distance = (radius * FIT_MARGIN) / Math.sin((camera.fov * Math.PI) / 360);
  camera.near = distance * NEAR_RATIO;
  camera.far = distance * FAR_RATIO;
  camera.position.copy(center).add(new Vector3(0, radius * 0.4, distance));
  camera.updateProjectionMatrix();
  controls.target.copy(center);
  controls.update();
  controls.saveState();
}

var described = 0;

// The status line says how to drag and zoom, so the canvas that takes those keys points to it.
function describe(figure, canvas) {
  var status = figure.querySelector("[role=status]");
  described += 1;
  status.id = status.id || "model-viewer-status-" + described;
  canvas.setAttribute("aria-describedby", status.id);
}

export function mount(figure) {
  var canvas = figure.querySelector("canvas");
  describe(figure, canvas);
  var renderer;
  try {
    renderer = new WebGLRenderer({ canvas: canvas, antialias: true, alpha: true });
  } catch (missing) {
    console.error("WebGL is not available", missing);
    say(figure, "error");
    return;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, MAX_PIXEL_RATIO));
  var scene = new Scene();
  var camera = new PerspectiveCamera(FIELD_OF_VIEW, 1);
  var controls = new OrbitControls(camera, canvas);
  controls.listenToKeyEvents(canvas);
  lights(scene, renderer);

  function draw() {
    renderer.render(scene, camera);
  }

  function resize() {
    var width = canvas.clientWidth;
    var height = canvas.clientHeight;
    if (!width || !height) {
      return;
    }
    renderer.setSize(width, height, false);
    camera.aspect = width / height;
    camera.updateProjectionMatrix();
    draw();
  }

  controls.addEventListener("change", draw);
  var sizes = new ResizeObserver(resize);
  sizes.observe(canvas);
  figure.querySelector("[data-model-reset]").addEventListener("click", function () {
    controls.reset();
  });
  // A swap that takes the viewer along frees its WebGL context, which browsers ration; other
  // swaps, such as quick search results, leave it drawing.
  function release(event) {
    if (!event.detail.target.contains(figure)) {
      return;
    }
    document.removeEventListener("htmx:beforeSwap", release);
    sizes.disconnect();
    controls.dispose();
    renderer.dispose();
    renderer.forceContextLoss();
  }
  document.addEventListener("htmx:beforeSwap", release);

  var choose = LOADERS[figure.getAttribute("data-format")];
  if (!choose) {
    say(figure, "error");
    return;
  }
  choose()
    .then(function (chosen) {
      return chosen.loader.loadAsync(figure.getAttribute("data-src")).then(chosen.root);
    })
    .then(function (model) {
      scene.add(model);
      frame(model, camera, controls);
      resize();
      say(figure, "ready");
    })
    .catch(function (failure) {
      // The page says the model could not be shown; the console keeps why, for whoever debugs it.
      console.error("3D model could not be shown", failure);
      say(figure, "error");
    });
}

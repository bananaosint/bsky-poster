// P5.js Color Flasher — cycles through all hues at max speed

let hue = 0;

function setup() {
    createCanvas(windowWidth, windowHeight);
    colorMode(HSB, 360, 100, 100);
    noStroke();
}

function draw() {
    // Cycle hue rapidly — jump by 5 each frame for speed
    hue = (hue + 5) % 360;
    background(hue, 100, 100);
}

function windowResized() {
    resizeCanvas(windowWidth, windowHeight);
}

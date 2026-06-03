// Simple enclosure shell for 7-inch 1024x600 display board.
// Adjust dimensions according to your exact PCB and bezel measurements.

$fn = 64;

panel_w = 165;
panel_h = 102;
panel_t = 4;

wall = 2.4;
depth = 18;
bezel = 9;

camera_hole_d = 8;
camera_hole_x = 0;
camera_hole_y = 38;

module rounded_box(w, h, d, r) {
    hull() {
        for (x = [-w/2 + r, w/2 - r])
        for (y = [-h/2 + r, h/2 - r])
        translate([x, y, 0])
            cylinder(h = d, r = r);
    }
}

difference() {
    rounded_box(panel_w + (2 * wall), panel_h + (2 * wall), depth, 5);
    translate([0, 0, wall])
        rounded_box(panel_w, panel_h, depth, 3.5);

    // Front window for active display area.
    translate([0, 0, -0.1])
        cube([panel_w - (2 * bezel), panel_h - (2 * bezel), wall + 0.2], center = true);

    // Camera cutout.
    translate([camera_hole_x, camera_hole_y, -0.1])
        cylinder(h = wall + 0.2, d = camera_hole_d);
}

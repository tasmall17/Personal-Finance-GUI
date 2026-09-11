// Read text out of a receipt image using Vision, which ships with macOS.
// Nothing is installed and nothing leaves the machine.
//
//   swift db/ocr_image.swift <image-file>
//
// Prints recognised lines in reading order, one per line.
//
// The input is redrawn into a plain 8-bit RGB bitmap first. Vision silently
// returns zero results for some colour spaces and alpha configurations - a PNG
// rendered from a PDF by sips is one - and gives no error when it does.
import Foundation
import Vision
import AppKit

let args = CommandLine.arguments
guard args.count > 1 else {
    FileHandle.standardError.write("usage: ocr_image.swift <image>\n".data(using: .utf8)!)
    exit(2)
}
guard let img = NSImage(contentsOfFile: args[1]),
      let src = img.cgImage(forProposedRect: nil, context: nil, hints: nil) else {
    FileHandle.standardError.write("could not read image: \(args[1])\n".data(using: .utf8)!)
    exit(1)
}

// redraw into a known-good bitmap: sRGB, 8 bits, no alpha
let w = src.width, h = src.height
guard let ctx = CGContext(data: nil, width: w, height: h, bitsPerComponent: 8,
                          bytesPerRow: 0, space: CGColorSpaceCreateDeviceRGB(),
                          bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue) else {
    FileHandle.standardError.write("could not make bitmap context\n".data(using: .utf8)!)
    exit(1)
}
ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
ctx.fill(CGRect(x: 0, y: 0, width: w, height: h))
ctx.draw(src, in: CGRect(x: 0, y: 0, width: w, height: h))
guard let cg = ctx.makeImage() else {
    FileHandle.standardError.write("could not flatten image\n".data(using: .utf8)!)
    exit(1)
}

var request = RecognizeTextRequest()
request.recognitionLevel = .accurate
request.usesLanguageCorrection = false      // receipts are codes, not prose

let obs = try await request.perform(on: cg)
if obs.isEmpty {
    FileHandle.standardError.write("no text found in \(args[1])\n".data(using: .utf8)!)
    exit(3)
}

// Vision returns boxes in no useful order. Group them into visual rows, then
// sort left to right, so the output reads the way the receipt does.
struct Box { let text: String; let y: CGFloat; let x: CGFloat }
var boxes: [Box] = []
for o in obs {
    guard let top = o.topCandidates(1).first else { continue }
    let bb = o.boundingBox.cgRect
    boxes.append(Box(text: top.string, y: bb.midY, x: bb.minX))
}
boxes.sort { $0.y == $1.y ? $0.x < $1.x : $0.y > $1.y }

var out: [String] = []
var row: [Box] = []
for b in boxes {
    if let first = row.first, abs(first.y - b.y) > 0.006 {
        out.append(row.sorted { $0.x < $1.x }.map { $0.text }.joined(separator: "  "))
        row = []
    }
    row.append(b)
}
if !row.isEmpty { out.append(row.sorted { $0.x < $1.x }.map { $0.text }.joined(separator: "  ")) }
print(out.joined(separator: "\n"))

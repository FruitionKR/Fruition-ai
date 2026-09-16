use docling_pdf::layout::LayoutModel;
use docling_pdf::PdfDocument;
use std::time::Instant;

fn main() {
    let mut model = LayoutModel::load().expect("layout model");
    for path in std::env::args().skip(1) {
        let bytes = std::fs::read(&path).expect("read pdf");
        let document = PdfDocument::open(&bytes, None).expect("open pdf");
        for (page_index, page) in document.pages.iter().enumerate() {
            let started = Instant::now();
            let regions = model
                .predict(docling_pdf::layout_src(page), page.width, page.height)
                .expect("layout prediction");
            eprintln!(
                "TIMING\t{}\t{}\t{:.6}",
                path,
                page_index + 1,
                started.elapsed().as_secs_f64()
            );
            for region in regions {
                if region.score >= 0.6
                    && matches!(region.label, "formula" | "table" | "picture")
                {
                    println!(
                        "REGION\t{}\t{}\t{}\t{:.6}\t{:.3}\t{:.3}\t{:.3}\t{:.3}",
                        path,
                        page_index + 1,
                        region.label,
                        region.score,
                        region.l,
                        region.t,
                        region.r,
                        region.b
                    );
                }
            }
        }
    }
}

"""Small real OOXML/PDF documents for byte-preserving download acceptance."""

from io import BytesIO
from zipfile import ZipFile, ZIP_DEFLATED


MIMES = {
	"docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
	"pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
	"xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
	"pdf": "application/pdf",
	"xml": "application/xml",
}


def samples():
	import pymupdf

	result = {}
	packages = {
		"docx": ("word/document.xml", "wordprocessingml.document.main+xml", {
			"word/document.xml": '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"><w:body><w:p><w:r><w:t>Drive invoice DOCX</w:t></w:r></w:p><w:sectPr/></w:body></w:document>',
		}),
		"xlsx": ("xl/workbook.xml", "spreadsheetml.sheet.main+xml", {
			"xl/workbook.xml": '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><sheets><sheet name="Invoice" sheetId="1" r:id="rId1"/></sheets></workbook>',
			"xl/_rels/workbook.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" Target="worksheets/sheet1.xml"/></Relationships>',
			"xl/worksheets/sheet1.xml": '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>Drive invoice XLSX</t></is></c></row></sheetData></worksheet>',
		}),
		"pptx": ("ppt/presentation.xml", "presentationml.presentation.main+xml", {
			"ppt/presentation.xml": '<p:presentation xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"><p:sldIdLst><p:sldId id="256" r:id="rId1"/></p:sldIdLst><p:sldSz cx="9144000" cy="6858000"/><p:notesSz cx="6858000" cy="9144000"/></p:presentation>',
			"ppt/_rels/presentation.xml.rels": '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/></Relationships>',
			"ppt/slides/slide1.xml": '<p:sld xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:cSld><p:spTree><p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr><p:grpSpPr/><p:sp><p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Drive invoice PPTX</a:t></a:r></a:p></p:txBody></p:sp></p:spTree></p:cSld></p:sld>',
		}),
	}
	for extension, (main, subtype, parts) in packages.items():
		stream = BytesIO()
		overrides = f'<Override PartName="/{main}" ContentType="application/vnd.openxmlformats-officedocument.{subtype}"/>'
		if extension == "xlsx":
			overrides += '<Override PartName="/xl/worksheets/sheet1.xml" ContentType="application/vnd.openxmlformats-officedocument.spreadsheetml.worksheet+xml"/>'
		if extension == "pptx":
			overrides += '<Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
		with ZipFile(stream, "w", ZIP_DEFLATED) as archive:
			archive.writestr("[Content_Types].xml", '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"><Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/><Default Extension="xml" ContentType="application/xml"/>' + overrides + '</Types>')
			archive.writestr("_rels/.rels", '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="' + main + '"/></Relationships>')
			for name, data in parts.items():
				archive.writestr(name, data)
		result[extension] = stream.getvalue()
	with pymupdf.open() as pdf:
		pdf.new_page().insert_text((72, 72), "Drive invoice PDF")
		result["pdf"] = pdf.tobytes()
	result["xml"] = b'<?xml version="1.0"?><invoice>Drive XML</invoice>'
	return result

import re
import os
import shutil
import tempfile
from PyQt4.QtCore import QSize, QVariant
import time
from qgis.core import (QgsVectorLayer,
                       QgsVectorFileWriter,
                       QgsPalLayerSettings,
                       QgsSingleSymbolRendererV2,
                       QgsCategorizedSymbolRendererV2,
                       QgsGraduatedSymbolRendererV2,
                       QgsRuleBasedRendererV2,
                       QgsHeatmapRenderer,
                       QgsSymbolLayerV2Utils,
                       QgsRasterFileWriter,
                       QgsRasterPipe,
                       QgsCoordinateReferenceSystem,
                       QgsCoordinateTransform,
                       QgsDataSourceURI,
                       QgsField,
                       QgsRenderContext,
                       QgsExpression,
                       QgsExpressionContext,
                       QgsExpressionContextUtils)
from qgis.utils import QGis
import processing
from leafletStyleScripts import getLayerStyle
from leafletScriptStrings import (popupScript,
                                  popFuncsScript,
                                  pointToLayerFunction,
                                  wfsScript,
                                  clusterScript,
                                  iconLegend)
from exp2js import compile_to_file
from utils import (writeTmpLayer, removeSpaces, exportImages, is25d,
                   handleHiddenField, add25dAttributes, BLEND_MODES)


def writeVectorLayer(layer, safeLayerName, usedFields, highlight,
                     popupsOnHover, popup, outputProjectFileName, wfsLayers,
                     cluster, visible, json, legends, new_src, canvas, zIndex,
                     restrictToExtent, extent, feedback):
    feedback.showFeedback("Writing %s as JSON..." % layer.name())
    zIndex = zIndex + 400
    markerFolder = os.path.join(outputProjectFileName, "markers")
    labeltext = getLabels(layer, safeLayerName, outputProjectFileName)
    (new_pop, popFuncs) = getPopups(layer, safeLayerName, highlight,
                                    popupsOnHover, popup)
    renderer = layer.rendererV2()
    layer_transp = 1 - (float(layer.layerTransparency()) / 100)
    style = ""

    if is25d(layer, canvas, restrictToExtent, extent):
        shadows = ""
        renderer = layer.rendererV2()
        renderContext = QgsRenderContext.fromMapSettings(canvas.mapSettings())
        fields = layer.pendingFields()
        renderer.startRender(renderContext, fields)
        for feat in layer.getFeatures():
            if isinstance(renderer, QgsCategorizedSymbolRendererV2):
                classAttribute = renderer.classAttribute()
                attrValue = feat.attribute(classAttribute)
                catIndex = renderer.categoryIndexForValue(attrValue)
                categories = renderer.categories()
                symbol = categories[catIndex].symbol()
            elif isinstance(renderer, QgsGraduatedSymbolRendererV2):
                classAttribute = renderer.classAttribute()
                attrValue = feat.attribute(classAttribute)
                ranges = renderer.ranges()
                for range in ranges:
                    if (attrValue >= range.lowerValue() and
                            attrValue <= range.upperValue()):
                        symbol = range.symbol().clone()
            else:
                symbol = renderer.symbolForFeature2(feat, renderContext)
            symbolLayer = symbol.symbolLayer(0)
            if not symbolLayer.paintEffect().effectList()[0].enabled():
                shadows = "'2015-07-15 10:00:00'"
        renderer.stopRender(renderContext)
        new_obj = """
        var osmb = new OSMBuildings(map).date(new Date({shadows}));
        osmb.set(json_{sln});""".format(shadows=shadows, sln=safeLayerName)
    elif isinstance(renderer, QgsHeatmapRenderer):
        (new_obj, legends,
         wfsLayers) = heatmapLayer(layer, safeLayerName, renderer, legends,
                                   wfsLayers)
    elif isinstance(renderer, QgsSingleSymbolRendererV2):
        (style, markerType) = getLayerStyle(layer, safeLayerName, markerFolder,
                                            outputProjectFileName)
        (new_obj, legends,
         wfsLayers) = singleLayer(renderer, outputProjectFileName,
                                  safeLayerName, wfsLayers, layer, labeltext,
                                  cluster, json, usedFields, legends,
                                  markerType)
    elif isinstance(renderer, QgsCategorizedSymbolRendererV2):
        (style, markerType) = getLayerStyle(layer, safeLayerName, markerFolder,
                                            outputProjectFileName)
        (new_obj, legends,
         wfsLayers) = categorizedLayer(layer, renderer, safeLayerName,
                                       outputProjectFileName, usedFields,
                                       legends, labeltext, cluster, json,
                                       wfsLayers, markerType)
    elif isinstance(renderer, QgsGraduatedSymbolRendererV2):
        (style, markerType) = getLayerStyle(layer, safeLayerName, markerFolder,
                                            outputProjectFileName)
        (new_obj, legends,
         wfsLayers) = graduatedLayer(layer, safeLayerName, renderer,
                                     outputProjectFileName, labeltext, cluster,
                                     json, usedFields, legends, wfsLayers,
                                     markerType)
    elif isinstance(renderer, QgsRuleBasedRendererV2):
        (style, markerType) = getLayerStyle(layer, safeLayerName, markerFolder,
                                            outputProjectFileName)
        (new_obj, legends,
         wfsLayers) = ruleBasedLayer(layer, renderer, safeLayerName,
                                     outputProjectFileName, usedFields,
                                     legends, labeltext, cluster, json,
                                     wfsLayers, markerType)
    blend = BLEND_MODES[layer.blendMode()]
    new_obj = u"""{style}
        map.createPane('pane_{sln}');
        map.getPane('pane_{sln}').style.zIndex = {zIndex};
        map.getPane('pane_{sln}').style['mix-blend-mode'] = '{blend}';
        {new_obj}""".format(
            style=style, sln=safeLayerName, zIndex=zIndex,
            blend=blend, new_obj=new_obj)
    if usedFields != 0:
        new_src += new_pop.decode("utf-8")
    new_src += """
""" + new_obj
    if is25d(layer, canvas, restrictToExtent, extent):
        pass
    else:
        new_src += """
        bounds_group.addLayer(layer_""" + safeLayerName + """);"""
        if visible:
            if cluster is False:
                new_src += """
        map.addLayer(layer_""" + safeLayerName + """);"""
            else:
                new_src += """
        cluster_""" + safeLayerName + """.addTo(map);"""
    feedback.completeStep()
    return new_src, legends, wfsLayers


def getLabels(layer, safeLayerName, outputProjectFileName):
    label_exp = ''
    labeltext = ""
    f = ''
    palyr = QgsPalLayerSettings()
    palyr.readFromLayer(layer)
    if palyr.enabled and palyr.fieldName and palyr.fieldName != "":
        bgColor = palyr.shapeFillColor.name()
        borderWidth = palyr.shapeBorderWidth
        borderColor = palyr.shapeBorderColor.name()
        x = palyr.shapeSize.x()
        y = palyr.shapeSize.y()
        font = palyr.textFont
        fontSize = font.pointSize()
        fontFamily = font.family()
        fontItalic = font.italic()
        fontBold = font.bold()
        fontColor = palyr.textColor.name()
        fontUnderline = font.underline()
        xOffset = palyr.xOffset
        yOffset = palyr.yOffset
        styleStart = "'<div style=\"color: %s; font-size: %dpt; " % (
                fontColor, fontSize)
        if palyr.shapeDraw:
            styleStart += "background-color: %s; " % bgColor
            styleStart += "border: %dpx solid %s; " % (borderWidth,
                                                       borderColor)
            if palyr.shapeSizeType == 0:
                styleStart += "padding: %dpx %dpx; " % (y, x)
            if palyr.shapeSizeType == 1:
                styleStart += "width: %dpx; " % x
                styleStart += "height: %dpx; " % y
        if fontBold:
            styleStart += "font-weight: bold; "
        if fontItalic:
            styleStart += "font-style: italic; "
        styleStart += "font-family: \\'%s\\', sans-serif;\">' + " % fontFamily
        styleEnd = " + '</div>'"
        if palyr.isExpression and palyr.enabled:
            exprFilename = os.path.join(outputProjectFileName, "js",
                                        "qgis2web_expressions.js")
            name = compile_to_file(palyr.getLabelExpression(),
                                   "label_%s" % safeLayerName, "Leaflet",
                                   exprFilename)
            js = "%s(context)" % (name)
            js = js.strip()
            f = js
        else:
            f = "feature.properties['%s']" % handleHiddenField(layer,
                                                               palyr.fieldName)
        labeltext = ".bindTooltip((" + unicode(f)
        labeltext += " !== null?String(%s%s)%s:'')" % (
                styleStart, unicode(f), styleEnd)
        labeltext += ", {permanent: true, offset: [-0, -16], "
        labeltext += "className: 'css_%s'}" % safeLayerName
        labeltext += ").openTooltip();"
    else:
        labeltext = ""
    return labeltext


def getPopups(layer, safeLayerName, highlight, popupsOnHover, popup):
    palyr = QgsPalLayerSettings()
    palyr.readFromLayer(layer)
    fields = layer.pendingFields()
    field_names = popup.keys()
    field_vals = popup.values()
    html_prov = False
    f = palyr.fieldName
    table = ""
    for field in popup:
        tablestart = "'<table>\\"
        row = ""
        for field, val in zip(field_names, field_vals):
            fieldIndex = fields.indexFromName(unicode(field))
            formCfg = layer.editFormConfig()
            editorWidget = formCfg.widgetType(fieldIndex)
            if (editorWidget == QgsVectorLayer.Hidden or
                    editorWidget == 'Hidden'):
                continue

            row += """
                    <tr>\\"""
            if val == 'inline label':
                row += """
                        <th scope="row">"""
                row += layer.attributeDisplayName(fieldIndex)
                row += """</th>\\
                        <td>"""
            else:
                row += """
                        <td colspan="2">"""
            if val == "header label":
                row += '<strong>'
                row += layer.attributeDisplayName(fieldIndex)
                row += '</strong><br />'
            row += "' + "
            row += "(feature.properties[\'" + unicode(field) + "\'] "
            row += "!== null ? "

            if (editorWidget == QgsVectorLayer.Photo or
                    editorWidget == 'Photo'):
                row += "'<img src=\"images/' + "
                row += "String(feature.properties['" + unicode(field)
                row += "']).replace(/[\\\/:]/g, '_').trim()"
                row += " + '\">' : '') + '"
            else:
                row += "Autolinker.link("
                row += "String(feature.properties['" + unicode(field)
                row += "'])) : '') + '"

            row += """</td>\\
                    </tr>\\"""
        tableend = """
                </table>'"""
        table = tablestart + row + tableend
    if popup != 0 and table != "":
        popFuncs = popFuncsScript(table)
    else:
        popFuncs = ""
    new_pop = popupScript(safeLayerName, popFuncs, highlight, popupsOnHover)
    return new_pop, popFuncs


def singleLayer(renderer, outputProjectFileName, safeLayerName, wfsLayers,
                layer, labeltext, cluster, json, usedFields, legends,
                markerType):
    symbol = renderer.symbol()
    legendIcon = QgsSymbolLayerV2Utils.symbolPreviewPixmap(symbol,
                                                           QSize(16, 16))
    legendIcon.save(os.path.join(outputProjectFileName, "legend",
                                 safeLayerName + ".png"))
    legends[safeLayerName] = '<img src="legend/' + safeLayerName + '.png" /> '
    legends[safeLayerName] += layer.name()
    if layer.geometryType() == QGis.Point:
        (new_obj,
         wfsLayers) = pointLayer(layer, safeLayerName, labeltext, cluster,
                                 usedFields, json, wfsLayers, markerType,
                                 symbol)
    else:
        new_obj, wfsLayers = nonPointLayer(layer, safeLayerName, usedFields,
                                           json, wfsLayers, symbol)
    return new_obj, legends, wfsLayers


def categorizedLayer(layer, renderer, safeLayerName, outputProjectFileName,
                     usedFields, legends, labeltext, cluster, json, wfsLayers,
                     markerType):
    catLegend = layer.name().replace("'", "\\'") + "<br />"
    catLegend += "<table>"
    categories = renderer.categories()
    for cnt, cat in enumerate(categories):
        symbol = cat.symbol()
        catLegend = iconLegend(symbol, cat, outputProjectFileName,
                               safeLayerName, catLegend, cnt)
    catLegend += "</table>"
    if layer.geometryType() == QGis.Point:
        (new_obj,
         wfsLayers) = pointLayer(layer, safeLayerName, labeltext, cluster,
                                 usedFields, json, wfsLayers, markerType,
                                 symbol)
    else:
        (new_obj,
         wfsLayers) = nonPointLayer(layer, safeLayerName, usedFields, json,
                                    wfsLayers, symbol)
    legends[safeLayerName] = catLegend
    return new_obj, legends, wfsLayers


def graduatedLayer(layer, safeLayerName, renderer, outputProjectFileName,
                   labeltext, cluster, json, usedFields, legends, wfsLayers,
                   markerType):
    catLegend = layer.name() + "<br />"
    catLegend += "<table>"
    for cnt, r in enumerate(renderer.ranges()):
        symbol = r.symbol()
        catLegend = iconLegend(symbol, r, outputProjectFileName, safeLayerName,
                               catLegend, cnt)
    catLegend += "</table>"
    if layer.geometryType() == QGis.Point:
        (new_obj,
         wfsLayers) = pointLayer(layer, safeLayerName, labeltext, cluster,
                                 usedFields, json, wfsLayers, markerType,
                                 symbol)
    else:
        (new_obj,
         wfsLayers) = nonPointLayer(layer, safeLayerName, usedFields, json,
                                    wfsLayers, symbol)
    legends[safeLayerName] = catLegend
    return new_obj, legends, wfsLayers


def ruleBasedLayer(layer, renderer, safeLayerName, outputProjectFileName,
                   usedFields, legends, labeltext, cluster, json, wfsLayers,
                   markerType):
    catLegend = layer.name() + "<br />"
    catLegend += "<table>"
    root_rule = renderer.rootRule()
    rules = root_rule.children()
    for cnt, r in enumerate(rules):
        symbol = r.symbol()
        catLegend = iconLegend(symbol, r, outputProjectFileName, safeLayerName,
                               catLegend, cnt)
    catLegend += "</table>"
    if layer.geometryType() == QGis.Point:
        (new_obj,
         wfsLayers) = pointLayer(layer, safeLayerName, labeltext, cluster,
                                 usedFields, json, wfsLayers, markerType,
                                 symbol)
    else:
        (new_obj,
         wfsLayers) = nonPointLayer(layer, safeLayerName, usedFields, json,
                                    wfsLayers, symbol)
    legends[safeLayerName] = catLegend
    return new_obj, legends, wfsLayers


def pointLayer(layer, safeLayerName, labeltext, cluster, usedFields, json,
               wfsLayers, markerType, symbol):
    if layer.providerType() == 'WFS' and json is False:
        p2lf = ""
        for sl in xrange(symbol.symbolLayerCount()):
            p2lf += pointToLayerFunction(safeLayerName, labeltext, symbol, sl)
        (new_obj,
         scriptTag) = buildPointWFS(p2lf, safeLayerName, layer, cluster,
                                    symbol)
        wfsLayers += wfsScript(scriptTag)
    else:
        attrText = layer.attribution()
        attrUrl = layer.attributionUrl()
        layerAttr = '<a href="%s">%s</a>' % (attrUrl, attrText)
        new_obj = buildPointJSON(symbol, safeLayerName, labeltext,
                                 usedFields, markerType, layerAttr)
        if cluster:
            new_obj += clusterScript(safeLayerName)
    return new_obj, wfsLayers


def nonPointLayer(layer, safeLayerName, usedFields, json, wfsLayers, symbol):
    if layer.providerType() == 'WFS' and json is False:
        new_obj, scriptTag = buildNonPointWFS(safeLayerName, layer, symbol)
        wfsLayers += wfsScript(scriptTag)
    else:
        attrText = layer.attribution()
        attrUrl = layer.attributionUrl()
        layerAttr = '<a href="%s">%s</a>' % (attrUrl, attrText)
        new_obj = buildNonPointJSON(safeLayerName, usedFields, layerAttr,
                                    symbol)
    return new_obj, wfsLayers


def heatmapLayer(layer, safeLayerName, renderer, legends, wfsLayers):
    attrText = layer.attribution()
    if attrText != "":
        attrUrl = layer.attributionUrl()
        layerAttr = '<a href="%s">%s</a>' % (attrUrl, attrText)
    else:
        layerAttr = ""
    hmRadius = renderer.radius() * 2
    hmWeight = renderer.weightExpression()
    if hmWeight is not None and hmWeight != "":
        hmWeightId = layer.fieldNameIndex(hmWeight)
        hmWeightMax = layer.maximumValue(hmWeightId)
    else:
        hmWeight = ""
        hmWeightMax = 1
    colorRamp = renderer.colorRamp()
    hmStart = colorRamp.color1().name()
    hmEnd = colorRamp.color2().name()
    hmRamp = "{0: '" + hmStart + "', "
    hmStops = colorRamp.stops()
    for stop in hmStops:
        hmRamp += unicode(stop.offset) + ": '" + stop.color.name() + "', "
    hmRamp += "1: '" + hmEnd + "'}"
    new_obj = """
        var %(sln)s_hm = geoJson2heat(json_%(sln)s,
                                      '%(hmWeight)s');
        var layer_%(sln)s = new L.heatLayer(%(sln)s_hm, {
            attribution: '%(attr)s',
            radius: %(hmRadius)d,
            max: %(hmWeightMax)d,
            minOpacity: 1,
            gradient: %(hmRamp)s});
        """ % {"sln": safeLayerName, "hmWeight": hmWeight, "attr": layerAttr,
               "hmWeightMax": hmWeightMax, "hmRamp": hmRamp,
               "hmRadius": hmRadius}
    return new_obj, legends, wfsLayers


def buildPointJSON(symbol, sln, label, usedFields, markerType, layerAttr):
    pointJSON = """
        var layer_{sln} = new L.geoJson.multiStyle(json_{sln}, {{
            attribution: '{attr}',
            pane: 'pane_{sln}',"""
    if usedFields != 0:
        pointJSON += """
            onEachFeature: pop_{sln},"""
    pointJSON += """
            pointToLayers: ["""
    for sl in xrange(symbol.symbolLayerCount()):
        pointJSON += """function (feature, latlng) {{
                var context = {{
                    feature: feature,
                    variables: {{}}
                }};
                return L.{markerType}(latlng, """
        pointJSON += """style_{sln}_%s(feature)){label}
            }},""" % sl
    pointJSON += """
        ]}});"""
    pointJSON = pointJSON.format(sln=sln, label=label, markerType=markerType,
                                 attr=layerAttr)
    return pointJSON


def buildPointWFS(p2lf, layerName, layer, cluster_set, symbol):
    attrText = layer.attribution()
    attrUrl = layer.attributionUrl()
    layerAttr = '<a href="%s">%s</a>' % (attrUrl, attrText)
    scriptTag = getWFSScriptTag(layer, layerName)
    p2ls = ""
    for sl in xrange(symbol.symbolLayerCount()):
        p2ls += "pointToLayer_%s_%s, " % (layerName, sl)
    new_obj = p2lf + """
        var layer_{layerName} = L.geoJson.multiStyle(null, {{
            attribution: '{layerAttr}',
            pane: 'pane_{layerName}',
            pointToLayers: [{p2ls}],
            onEachFeature: pop_{layerName}
        }});""".format(layerName=layerName, layerAttr=layerAttr, p2ls=p2ls)
    if cluster_set:
        new_obj += """
        var cluster_{layerName} = """.format(layerName=layerName)
        new_obj += "new L.MarkerClusterGroup({showCoverageOnHover: false});"
    new_obj += """
        function get{layerName}Json(geojson) {{
            layer_{layerName}""".format(layerName=layerName)
    new_obj += ".addData(geojson);"
    if cluster_set:
        new_obj += """
            cluster_{layerName}.add""".format(layerName=layerName)
        new_obj += "Layer(layer_{layerName});".format(layerName=layerName)
    new_obj += """
            setBounds();
        };"""
    return new_obj, scriptTag


def buildNonPointJSON(safeName, usedFields, layerAttr, symbol):
    if usedFields != 0:
        onEachFeature = """
        onEachFeature: pop_{safeName},""".format(safeName=safeName)
    else:
        onEachFeature = ""
    styles = ""
    for sl in xrange(symbol.symbolLayerCount()):
        styles += """style_%s_%s,""" % (safeName, sl)
    new_obj = """
    var layer_{safeName} = new L.geoJson.multiStyle(json_{safeName}, {{
        attribution: '{attr}',
        pane: 'pane_{safeName}',{onEachFeature}
        styles: [{styles}]
    }});"""
    new_obj = new_obj.format(safeName=safeName, attr=layerAttr,
                             onEachFeature=onEachFeature, styles=styles)
    return new_obj


def buildNonPointWFS(layerName, layer, symbol):
    attrText = layer.attribution()
    attrUrl = layer.attributionUrl()
    layerAttr = '<a href="%s">%s</a>' % (attrUrl, attrText)
    scriptTag = getWFSScriptTag(layer, layerName)
    styles = ""
    for sl in xrange(symbol.symbolLayerCount()):
        styles += """style_%s_%s,""" % (layerName, sl)
    new_obj = """
        var layer_{layerName} = L.geoJson.multiStyle(null, {{
            attribution: '{attr}',
            styles: [{styles}],
            pane: 'pane_{layerName}',
            onEachFeature: pop_{layerName}
        }});"""
    new_obj += """
        function get{layerName}Json(geojson) {{
            layer_{layerName}"""
    new_obj = new_obj.format(layerName=layerName, attr=layerAttr,
                             styles=styles)
    new_obj += ".addData(geojson);"
    new_obj += """
        };"""
    return new_obj, scriptTag


def getWFSScriptTag(layer, layerName):
    layerSource = layer.source()
    if ("retrictToRequestBBOX" in layerSource or
            "restrictToRequestBBOX" in layerSource):
        provider = layer.dataProvider()
        uri = QgsDataSourceURI(provider.dataSourceUri())
        wfsURL = uri.param("url")
        wfsTypename = uri.param("typename")
        wfsSRS = uri.param("srsname")
        layerSource = wfsURL
        layerSource += "?SERVICE=WFS&VERSION=1.0.0&"
        layerSource += "REQUEST=GetFeature&TYPENAME="
        layerSource += wfsTypename
        layerSource += "&SRSNAME="
        layerSource += wfsSRS
    scriptTag = re.sub('SRSNAME\=EPSG\:\d+', 'SRSNAME=EPSG:4326', layerSource)
    scriptTag += "&outputFormat=text%2Fjavascript&format_options=callback%3A"
    scriptTag += "get" + layerName + "Json"
    return scriptTag

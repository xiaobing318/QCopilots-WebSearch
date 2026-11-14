#include <qws/html.hpp>
#include <QSet>
#include <QStringList>
#include <QUrl>
#include <algorithm>
#include <array>
#include <cstring>
#include <optional>
#include <string_view>
#include <vector>
#include <lexbor/html/html.h>
#include <lexbor/dom/interfaces/character_data.h>
#include <lexbor/dom/interfaces/element.h>
#include <lexbor/encoding/encoding.h>
namespace qws {
namespace {
using Node=lxb_dom_node_t;
using Document=std::unique_ptr<lxb_html_document_t,decltype(&lxb_html_document_destroy)>;
[[noreturn]] void encodingError(){throw Error("unsupported_content","Unsupported charset or invalid encoded text.",415);}
[[noreturn]] void tooLarge(){throw Error("response_too_large","Page exceeds extraction complexity limits.",413);}
QString utf8(const lxb_char_t *p,size_t n){return p?QString::fromUtf8(reinterpret_cast<const char*>(p),qsizetype(n)):QString();}
QString attribute(Node *n,const char *name) {
  size_t len=0;const auto *p=lxb_dom_element_get_attribute(reinterpret_cast<lxb_dom_element_t*>(n),
    reinterpret_cast<const lxb_char_t*>(name),std::strlen(name),&len);return utf8(p,len);
}
std::optional<QString> charsetParameter(const QString &input) {
  qsizetype i=0;
  while(i<input.size()) {
    while(i<input.size()&&(input[i].isSpace()||input[i]==';')) ++i;
    const auto begin=i;while(i<input.size()&&input[i]!='='&&input[i]!=';') ++i;
    const auto key=input.mid(begin,i-begin).trimmed();
    if(i==input.size()) break;
    if(input[i]==';'){++i;continue;}
    ++i;while(i<input.size()&&input[i].isSpace()) ++i;
    QString value;bool valid=true;
    if(i<input.size()&&(input[i]=='"'||input[i]=='\'')) {
      const auto quote=input[i++];bool closed=false;
      while(i<input.size()) {auto c=input[i++];if(c==quote){closed=true;break;}if(c=='\\'&&i<input.size()) c=input[i++];value.append(c);}
      valid=closed;while(i<input.size()&&input[i]!=';'){if(!input[i].isSpace()) valid=false;++i;}
    } else {const auto start=i;while(i<input.size()&&input[i]!=';') ++i;value=input.mid(start,i-start).trimmed();}
    if(key.compare("charset",Qt::CaseInsensitive)==0) return valid?value.trimmed():QString();
  }
  return std::nullopt;
}
std::optional<QString> earlyMetaCharset(const std::string &body) {
  Document doc(lxb_html_document_create(),lxb_html_document_destroy);
  if(!doc) encodingError();
  lxb_html_document_scripting_set(doc.get(),true);
  if(lxb_html_document_parse(doc.get(),reinterpret_cast<const lxb_char_t*>(body.data()),std::min<std::size_t>(body.size(),1024))!=LXB_STATUS_OK) encodingError();
  std::vector<Node*> pending{lxb_dom_interface_node(doc.get())};
  while(!pending.empty()) {
    auto *n=pending.back();pending.pop_back();
    if(n->type==LXB_DOM_NODE_TYPE_ELEMENT) {
      switch(n->local_name){case LXB_TAG_SCRIPT:case LXB_TAG_STYLE:case LXB_TAG_NOSCRIPT:case LXB_TAG_TEMPLATE:continue;default:break;}
      if(n->local_name==LXB_TAG_META) {
        const auto direct=attribute(n,"charset").trimmed();if(!direct.isEmpty()) return direct;
        if(attribute(n,"http-equiv").trimmed().compare("content-type",Qt::CaseInsensitive)==0)
          if(auto label=charsetParameter(attribute(n,"content"));label&&!label->isEmpty()) return label;
      }
    }
    for(auto *c=n->last_child;c;c=c->prev) pending.push_back(c);
  }
  return std::nullopt;
}
struct EncodingChoice {const lxb_encoding_data_t *data;std::size_t bomBytes=0;};
EncodingChoice chooseEncoding(const std::string &body,const std::string &contentType,bool html) {
  const std::string_view bytes(body);
  if(bytes.starts_with(std::string_view("\xff\xfe\0\0",4))||bytes.starts_with(std::string_view("\0\0\xfe\xff",4))) encodingError();
  if(bytes.starts_with(std::string_view("\xef\xbb\xbf",3))) return {lxb_encoding_data(LXB_ENCODING_UTF_8),3};
  if(bytes.starts_with(std::string_view("\xff\xfe",2))) return {lxb_encoding_data(LXB_ENCODING_UTF_16LE),2};
  if(bytes.starts_with(std::string_view("\xfe\xff",2))) return {lxb_encoding_data(LXB_ENCODING_UTF_16BE),2};
  auto label=charsetParameter(QString::fromStdString(contentType));bool fromMeta=false;
  if(!label&&html){label=earlyMetaCharset(body);fromMeta=label.has_value();}
  if(!label) return {lxb_encoding_data(LXB_ENCODING_UTF_8),0};
  const auto name=label->trimmed().toLatin1();const auto *p=reinterpret_cast<const lxb_char_t*>(name.constData());
  const auto *data=fromMeta?lxb_encoding_data_prescan_validate(p,size_t(name.size())):lxb_encoding_data_by_pre_name(p,size_t(name.size()));
  if(!data||!data->decode||data->encoding==LXB_ENCODING_DEFAULT||data->encoding==LXB_ENCODING_AUTO||
     data->encoding==LXB_ENCODING_UNDEFINED||data->encoding==LXB_ENCODING_REPLACEMENT) encodingError();
  return {data,0};
}
QString decode(std::string_view bytes,EncodingChoice choice,bool truncated) {
  if(bytes.size()<choice.bomBytes) encodingError();bytes.remove_prefix(choice.bomBytes);
  const auto *cursor=reinterpret_cast<const lxb_char_t*>(bytes.data());const auto *end=cursor+bytes.size();
  std::array<lxb_codepoint_t,4096> buffer{};lxb_encoding_decode_t state{};
  if(lxb_encoding_decode_init(&state,choice.data,buffer.data(),buffer.size())!=LXB_STATUS_OK) encodingError();
  QString result;result.reserve(qsizetype(bytes.size()));
  for(;;) {
    const auto *before=cursor;lxb_encoding_decode_buf_used_set(&state,0);
    const auto status=choice.data->decode(&state,&cursor,end);
    if(status!=LXB_STATUS_OK&&status!=LXB_STATUS_SMALL_BUFFER&&status!=LXB_STATUS_CONTINUE) encodingError();
    const auto count=lxb_encoding_decode_buf_used(&state);
    for(size_t i=0;i<count;++i) {
      const char32_t cp=buffer[i];if(cp>0x10ffff||(cp>=0xd800&&cp<=0xdfff)) encodingError();
      if(cp<=0xffff) result.append(QChar(char16_t(cp)));
      else {result.append(QChar(QChar::highSurrogate(cp)));result.append(QChar(QChar::lowSurrogate(cp)));}
    }
    if(status==LXB_STATUS_SMALL_BUFFER){if(cursor==before&&count==0) encodingError();continue;}
    if(cursor!=end) encodingError();
    if(!(truncated&&status==LXB_STATUS_CONTINUE)&&lxb_encoding_decode_finish(&state)!=LXB_STATUS_OK) encodingError();
    break;
  }
  return result;
}
bool hidden(Node *n) {
  if(n->type!=LXB_DOM_NODE_TYPE_ELEMENT) return false;
  switch(n->local_name){case LXB_TAG_SCRIPT:case LXB_TAG_STYLE:case LXB_TAG_NAV:case LXB_TAG_HEADER:case LXB_TAG_FOOTER:
    case LXB_TAG_TEMPLATE:case LXB_TAG_NOSCRIPT:case LXB_TAG_HEAD:return true;default:break;}
  if(lxb_dom_element_has_attribute(reinterpret_cast<lxb_dom_element_t*>(n),reinterpret_cast<const lxb_char_t*>("hidden"),6)) return true;
  if(attribute(n,"aria-hidden").trimmed().compare("true",Qt::CaseInsensitive)==0) return true;
  for(const auto &p:attribute(n,"style").toLower().split(';')) {
    const auto colon=p.indexOf(':');if(colon<0) continue;
    const auto k=p.left(colon).trimmed(),v=p.mid(colon+1).trimmed();
    if((k=="display"&&v.startsWith("none"))||(k=="visibility"&&v.startsWith("hidden"))) return true;
  }
  return false;
}
bool block(Node *n) {
  switch(n->local_name){case LXB_TAG_P:case LXB_TAG_DIV:case LXB_TAG_BR:case LXB_TAG_HR:case LXB_TAG_SECTION:case LXB_TAG_ARTICLE:
    case LXB_TAG_MAIN:case LXB_TAG_LI:case LXB_TAG_UL:case LXB_TAG_OL:case LXB_TAG_PRE:case LXB_TAG_BLOCKQUOTE:case LXB_TAG_TR:
    case LXB_TAG_TD:case LXB_TAG_TH:case LXB_TAG_DT:case LXB_TAG_DD:
    case LXB_TAG_H1:case LXB_TAG_H2:case LXB_TAG_H3:case LXB_TAG_H4:case LXB_TAG_H5:case LXB_TAG_H6:return true;default:return false;}
}
struct Output {
  QString value;qsizetype scalarCount=0,lineScalars=0;int lines=1;bool pendingSpace=false,full=false;
  void boundary() {
    pendingSpace=false;if(full||value.isEmpty()||value.endsWith('\n')) return;
    if(lines>=10000||scalarCount>=200000){full=true;return;}
    value.append('\n');++scalarCount;++lines;lineScalars=0;
  }
  void put(char32_t cp) {
    if(full) return;if(lineScalars>=1000) boundary();if(full) return;
    if(scalarCount>=200000){full=true;return;}
    if(cp<=0xffff) value.append(QChar(char16_t(cp)));
    else {value.append(QChar(QChar::highSurrogate(cp)));value.append(QChar(QChar::lowSurrogate(cp)));}
    ++scalarCount;++lineScalars;
  }
  void append(const QString &s,bool preserveLines=false) {
    for(qsizetype i=0;i<s.size()&&!full;++i) {
      char32_t cp=s[i].unicode();
      if(s[i].isHighSurrogate()&&i+1<s.size()&&s[i+1].isLowSurrogate()){cp=QChar::surrogateToUcs4(s[i],s[i+1]);++i;}
      else if(s[i].isSurrogate()) cp=0xfffd;
      if(preserveLines&&(cp=='\r'||cp=='\n')){if(cp=='\r'&&i+1<s.size()&&s[i+1]=='\n') ++i;boundary();continue;}
      if(QChar::isSpace(cp)){pendingSpace=lineScalars>0;continue;}
      if(cp==0||QChar::category(cp)==QChar::Other_Control) continue;
      if(pendingSpace&&lineScalars>=999) boundary();
      if(pendingSpace&&lineScalars>0) put(U' ');
      pendingSpace=false;put(cp);
    }
  }
};
struct Frame {Node *node;unsigned depth;bool exit=false,pre=false;};
}
HtmlDocument parseHtml(const std::string &body,const std::string &url,const std::string &contentType) {
  HtmlDocument out;
  const auto mime=QString::fromStdString(contentType).section(';',0,0).trimmed().toLower();
  const bool plain=mime=="text/plain";
  if(!plain&&mime!="text/html"&&mime!="application/xhtml+xml") throw Error("unsupported_content","Only HTML and plain text are supported.",415);
  out.truncated=!plain&&body.size()>1024*1024;
  const std::string_view input(body.data(),out.truncated?1024*1024:body.size());
  const auto decoded=decode(input,chooseEncoding(body,contentType,!plain),out.truncated);
  Output text;
  if(plain){text.append(decoded,true);out.title=url;}
  else {
    const auto html=decoded.toUtf8();if(std::count(html.begin(),html.end(),'<')>50000) tooLarge();
    Document doc(lxb_html_document_create(),lxb_html_document_destroy);
    if(!doc||lxb_html_document_parse(doc.get(),reinterpret_cast<const lxb_char_t*>(html.constData()),size_t(html.size()))!=LXB_STATUS_OK)
      throw Error("unsupported_content","HTML parsing failed.",415);
    size_t length=0;const auto *title=lxb_html_document_title(doc.get(),&length);
    auto titleText=utf8(title,length).simplified().left(2048);if(!titleText.isEmpty()&&titleText.back().isHighSurrogate()) titleText.chop(1);
    out.title=titleText.toStdString();
    auto *root=lxb_dom_interface_node(lxb_html_document_body_element(doc.get()));
    if(!root) throw Error("unsupported_content","HTML has no readable body.",415);
    Node *main=nullptr,*article=nullptr;std::vector<Frame> scan{{root,0}};
    while(!scan.empty()) {
      const auto f=scan.back();scan.pop_back();if(f.depth>512) tooLarge();
      if(hidden(f.node)) continue;
      if(f.node->local_name==LXB_TAG_MAIN&&!main) main=f.node;
      if(f.node->local_name==LXB_TAG_ARTICLE&&!article) article=f.node;
      for(auto *c=f.node->last_child;c;c=c->prev) scan.push_back({c,f.depth+1});
    }
    root=main?main:article?article:root;
    std::vector<Frame> stack{{root,0}};QSet<QString> seen;QUrl base(QString::fromStdString(url),QUrl::StrictMode);
    std::vector<Node*> heads{lxb_dom_interface_node(doc.get())};bool baseFound=false;
    while(!heads.empty()&&!baseFound) {
      auto *n=heads.back();heads.pop_back();
      if(n->type==LXB_DOM_NODE_TYPE_ELEMENT) {
        if(n->local_name==LXB_TAG_TEMPLATE||n->local_name==LXB_TAG_NOSCRIPT) continue;
        if(n->local_name==LXB_TAG_BASE&&lxb_dom_element_has_attribute(reinterpret_cast<lxb_dom_element_t*>(n),reinterpret_cast<const lxb_char_t*>("href"),4)) {
          baseFound=true;const auto target=base.resolved(QUrl(attribute(n,"href"),QUrl::StrictMode));
          if(target.isValid()&&!target.host().isEmpty()&&target.userInfo().isEmpty()&&(target.scheme()=="http"||target.scheme()=="https")) base=target;
        }
      }
      for(auto *c=n->last_child;c;c=c->prev) heads.push_back(c);
    }
    while(!stack.empty()&&!text.full) {
      const auto f=stack.back();stack.pop_back();auto *n=f.node;
      if(f.exit){if(block(n)) text.boundary();continue;}
      if(hidden(n)) continue;
      if(n->type==LXB_DOM_NODE_TYPE_TEXT){const auto &d=reinterpret_cast<lxb_dom_character_data_t*>(n)->data;text.append(utf8(d.data,d.length),f.pre);continue;}
      if(block(n)) text.boundary();
      if(n->type==LXB_DOM_NODE_TYPE_ELEMENT&&n->local_name==LXB_TAG_A) {
        const auto href=attribute(n,"href");const auto link=base.resolved(QUrl(href,QUrl::StrictMode));const auto encoded=link.toString(QUrl::FullyEncoded);
        if(!href.isEmpty()&&link.isValid()&&!link.host().isEmpty()&&link.userInfo().isEmpty()&&(link.scheme()=="http"||link.scheme()=="https")&&
           encoded.size()<=4096&&!seen.contains(encoded)) {
          if(out.links.size()<200){seen.insert(encoded);out.links.push_back({{"url",encoded.toStdString()}});}else out.truncated=true;
        }
      }
      stack.push_back({n,f.depth,true,f.pre});
      for(auto *c=n->last_child;c;c=c->prev) stack.push_back({c,f.depth+1,false,f.pre||n->local_name==LXB_TAG_PRE});
    }
  }
  out.truncated=out.truncated||text.full;out.text=text.value.trimmed().toStdString();if(out.title.empty()) out.title=url;
  return out;
}
}

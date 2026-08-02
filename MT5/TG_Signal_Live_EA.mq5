//+------------------------------------------------------------------+
//|                                           TG_Signal_Live_EA.mq5  |
//|  Универсальный live-робот: канал Telegram → сделки MT5.          |
//|  Правила разбора: inputs ИЛИ файл                                |
//|    Common\Files\SignalKit\parse_rules.txt                        |
//|  (панель SignalKit пишет этот файл кнопкой «Подготовить Live»).  |
//|                                                                  |
//|  WebRequest: https://t.me                                        |
//+------------------------------------------------------------------+
#property copyright "SignalKit"
#property version   "1.20"
#property description "Universal TG→MT5 live + chains. Allow WebRequest https://t.me"

#include <Trade/Trade.mqh>
#include <Trade/PositionInfo.mqh>
#include <Trade/OrderInfo.mqh>

input group "=== Telegram ==="
input string InpChannel          = "";
input int    InpPollSeconds      = 10;
input bool   InpIgnoreHistory    = true;
input int    InpMaxSignalAgeSec  = 3600;
input bool   InpLoadRulesFile    = true;   // читать SignalKit\\parse_rules.txt

input group "=== Разбор поста (если нет файла) ==="
input string InpFormat           = "labels"; // labels | compact
input string InpMustContain      = "Стоп лосс";
input string InpSkipContains     = "фиксируем|переносим стоп|безубыток|меняем цену";
input string InpLabelSide        = "Тип сделки";
input string InpLabelEntry       = "Цена открытия";
input string InpLabelSL          = "Стоп лосс";
input string InpLabelTP          = "Тейк профит";
input string InpBuyWords         = "покуп|лонг|buy|long";
input string InpSellWords        = "продаж|шорт|sell|short";
input string InpLimitWords       = "лимит|limit";
input string InpTpOpenWords      = "открыт";
input double InpOpenTpRR         = 2.0;
input string InpCompactBuy       = "Buy";
input string InpCompactSell      = "Sell";
input string InpCompactSL        = "SL";
input string InpCompactTP        = "TP";
input bool   InpManageEnabled    = true;   // сопровождение сделок (цепочки)
input string InpInheritWords     = "не меняем|без изменений|параметры без|остальные параметры";
input string InpLevelsWords      = "меняем цену|цену открытия на|сменили цен|переставляем лимит";

input group "=== Торговля ==="
input double InpFixedLot         = 0.01;
input double InpRiskPercent      = 0;
input bool   InpAllowLimit       = true;
input int    InpSlippagePoints   = 50;
input long   InpMagic            = 26080299;
input string InpCommentPrefix    = "SK#";
input bool   InpSkipBadDirection = true;
input int    InpMaxStopAdjustPts = 80;
input bool   InpDryRun           = true;

input group "=== Символы / лог ==="
input string InpOnlySymbols      = "";
input string InpSymbolSuffix     = "";
input bool   InpAutoResolve      = true;
input bool   InpUseCommonFolder  = true;
input bool   InpVerboseParse     = false;

struct SSig
  {
   long     msg_id;
   datetime time_utc;
   string   symbol;
   string   text;       // исходный текст поста
   int      side;       // 1 buy / -1 sell
   int      is_limit;
   double   entry, sl, tp;
   bool     tp_open;
   bool     inherit;    // SL/TP взять из открытой позиции/лимита
   int      action;     // 0 open, 1 close, 2 be, 3 modify_sl, 4 modify_levels, 5 replace_market, 6 reverse, 7 cancel
  };

// runtime rules (from file or inputs)
string g_channel, g_format, g_must, g_skip;
string g_lab_side, g_lab_entry, g_lab_sl, g_lab_tp;
string g_buy, g_sell, g_limit, g_tp_open;
string g_cbuy, g_csell, g_csl, g_ctp;
string g_m_cancel, g_m_reverse, g_m_be, g_m_close, g_m_modify, g_m_market;
string g_m_inherit, g_m_levels, g_m_add, g_m_keep;
double g_rr;
bool   g_manage=true;
int    g_link_hours=720;

CTrade g_trade; CPositionInfo g_pos; COrderInfo g_ord;
string g_ck[], g_cv[];
long   g_done[];
long   g_seen_max_id=0;
bool   g_bootstrapped=false;
int    g_opened=0, g_skipped=0, g_failed=0, g_polls=0, g_http_err=0;

string BaseKey(string s)
  {
   StringTrimLeft(s); StringTrimRight(s); StringToUpper(s);
   int d=StringFind(s,"."); if(d>0) s=StringSubstr(s,0,d);
   string tails[]={"PRO","MINI","MICRO","RAW","ECN","STP"};
   for(int i=0;i<ArraySize(tails);i++)
     {
      int t=StringLen(tails[i]), n=StringLen(s);
      if(n>t && StringSubstr(s,n-t)==tails[i]) { s=StringSubstr(s,0,n-t); break; }
     }
   return s;
  }

bool InOnly(const string csv)
  {
   string list=InpOnlySymbols; StringTrimLeft(list); StringTrimRight(list);
   if(list=="") return true;
   StringToUpper(list);
   string key=BaseKey(csv), parts[];
   int n=StringSplit(list,',',parts);
   for(int i=0;i<n;i++)
     { StringTrimLeft(parts[i]); StringTrimRight(parts[i]);
       if(parts[i]!="" && BaseKey(parts[i])==key) return true; }
   return false;
  }

string CacheGet(const string k){ for(int i=0;i<ArraySize(g_ck);i++) if(g_ck[i]==k) return g_cv[i]; return NULL; }
void CacheSet(const string k,const string v){ int n=ArraySize(g_ck); ArrayResize(g_ck,n+1); ArrayResize(g_cv,n+1); g_ck[n]=k; g_cv[n]=v; }
bool SymOk(const string name){ if(name=="") return false; SymbolSelect(name,true); return SymbolInfoInteger(name,SYMBOL_EXIST)!=0; }

string Resolve(const string csv)
  {
   string c=CacheGet(csv); if(c!=NULL) return c;
   string up=csv; StringToUpper(up);
   string tries[4]; tries[0]=csv; tries[1]=up; tries[2]=csv+InpSymbolSuffix; tries[3]=up+InpSymbolSuffix;
   for(int i=0;i<4;i++) if(tries[i]!="" && SymOk(tries[i])){ CacheSet(csv,tries[i]); return tries[i]; }
   if(InpAutoResolve)
     {
      string want=BaseKey(csv);
      int total=SymbolsTotal(false);
      for(int i=0;i<total;i++)
        { string name=SymbolName(i,false); if(BaseKey(name)==want && SymOk(name)){ CacheSet(csv,name); return name; } }
     }
   CacheSet(csv,""); return "";
  }

bool IsDone(const long id){ for(int i=0;i<ArraySize(g_done);i++) if(g_done[i]==id) return true; return false; }

void MarkDone(const long id)
  {
   if(id<=0 || IsDone(id)) return;
   int n=ArraySize(g_done); ArrayResize(g_done,n+1); g_done[n]=id;
   int flags=FILE_READ|FILE_WRITE|FILE_TXT|FILE_UNICODE|FILE_SHARE_READ|FILE_SHARE_WRITE;
   if(InpUseCommonFolder) flags|=FILE_COMMON;
   int fh=FileOpen("SignalKit\\live_done.txt",flags);
   if(fh==INVALID_HANDLE)
     { flags=FILE_WRITE|FILE_TXT|FILE_UNICODE|FILE_SHARE_READ|FILE_SHARE_WRITE;
       if(InpUseCommonFolder) flags|=FILE_COMMON;
       fh=FileOpen("SignalKit\\live_done.txt",flags); }
   if(fh!=INVALID_HANDLE){ FileSeek(fh,0,SEEK_END); FileWriteString(fh,IntegerToString(id)+"\n"); FileClose(fh); }
  }

void LoadDone()
  {
   ArrayResize(g_done,0);
   int flags=FILE_READ|FILE_TXT|FILE_UNICODE|FILE_SHARE_READ;
   if(InpUseCommonFolder) flags|=FILE_COMMON;
   int fh=FileOpen("SignalKit\\live_done.txt",flags);
   if(fh==INVALID_HANDLE) return;
   while(!FileIsEnding(fh))
     { string line=FileReadString(fh); StringTrimLeft(line); StringTrimRight(line);
       long id=StringToInteger(line); if(id>0 && !IsDone(id)){ int n=ArraySize(g_done); ArrayResize(g_done,n+1); g_done[n]=id; } }
   FileClose(fh);
  }

string RuleGet(const string line,const string key)
  {
   string k=key+"=";
   if(StringFind(line,k)!=0) return "";
   return StringSubstr(line,StringLen(k));
  }

void InitRulesFromInputs()
  {
   g_channel=InpChannel; StringReplace(g_channel,"@",""); StringTrimLeft(g_channel); StringTrimRight(g_channel);
   g_format=InpFormat; StringToLower(g_format);
   g_must=InpMustContain; g_skip=InpSkipContains;
   g_lab_side=InpLabelSide; g_lab_entry=InpLabelEntry; g_lab_sl=InpLabelSL; g_lab_tp=InpLabelTP;
   g_buy=InpBuyWords; g_sell=InpSellWords; g_limit=InpLimitWords; g_tp_open=InpTpOpenWords;
   g_cbuy=InpCompactBuy; g_csell=InpCompactSell; g_csl=InpCompactSL; g_ctp=InpCompactTP;
   g_rr=InpOpenTpRR;
   g_manage=InpManageEnabled;
   g_link_hours=720;
   g_m_cancel="ЛИМИТНЫЙ ОРДЕР УДАЛЯЕМ|ОРДЕР УДАЛЯЕМ|ЛИМИТКУ УДАЛЯЕМ|УДАЛЯЕМ ЛИМИТ";
   g_m_reverse="ПЕРЕЗАХОДИМ|РАЗВОРОТ|В ПРОТИВОПОЛОЖ|ЗАКРЫВАЕМ ПРОДАЖУ И|ЗАКРЫВАЕМ ПОКУПКУ И";
   g_m_be="БЕЗУБЫТОК|СТОП В БЕ|ПЕРЕНОСИМ СТОП|НА ТОЧКУ ВХОДА|ТОЧКУ ВХОДА|ТОЧКИ ВХОДА|BREAKEVEN|BREAK EVEN";
   g_m_close="ЗАКРЫВАЕМ ПОЛНОСТЬЮ|ЗАКРЫВАЕМ СДЕЛКУ|ВЫХОДИМ ИЗ СДЕЛКИ|ФИКСИРУЕМ ВСЮ";
   g_m_modify="СТОП ЛОСС ПЕРЕМЕЩАЕМ|СТОП ЛОСС МЕНЯЕМ|МЕНЯЕМ СТОП|ВЫСТАВЛЯЕМ СТОП|СТОП ЛОСС ОБРАТНО";
   g_m_market="ПО РЫНКУ|ПО РЫНОЧН|РЫНОЧНОЙ ЦЕНЕ|ОТКРЫВАЕМСЯ ПО РЫНКУ|ЗАХОДИМ ПО РЫНКУ|ОТКРЫВАЕМ ПО РЫНКУ";
   g_m_inherit=InpInheritWords;
   g_m_levels=InpLevelsWords;
   g_m_add="УСРЕДН|ДОБИРАЕМ|ЕЩЕ ОДИН ОРДЕР|ЕЩЁ ОДИН ОРДЕР";
   g_m_keep="ЛИМИТКУ НЕ УДАЛЯЕМ|ЛИМИТ НЕ УДАЛЯЕМ|ОРДЕР НЕ УДАЛЯЕМ";
  }

void LoadRulesFile()
  {
   InitRulesFromInputs();
   if(!InpLoadRulesFile) return;
   int flags=FILE_READ|FILE_TXT|FILE_UNICODE|FILE_SHARE_READ;
   if(InpUseCommonFolder) flags|=FILE_COMMON;
   int fh=FileOpen("SignalKit\\parse_rules.txt",flags);
   if(fh==INVALID_HANDLE)
     { Print("parse_rules.txt не найден — используем inputs"); return; }
   while(!FileIsEnding(fh))
     {
      string line=FileReadString(fh);
      StringTrimLeft(line); StringTrimRight(line);
      if(line=="" || StringGetCharacter(line,0)==';') continue;
      string v;
      v=RuleGet(line,"channel"); if(v!="") g_channel=v;
      v=RuleGet(line,"format"); if(v!="") { g_format=v; StringToLower(g_format); }
      v=RuleGet(line,"must_contain"); if(v!="") g_must=v;
      v=RuleGet(line,"skip_if_contains"); if(v!="") g_skip=v;
      v=RuleGet(line,"label_side"); if(v!="") g_lab_side=v;
      v=RuleGet(line,"label_entry"); if(v!="") g_lab_entry=v;
      v=RuleGet(line,"label_sl"); if(v!="") g_lab_sl=v;
      v=RuleGet(line,"label_tp"); if(v!="") g_lab_tp=v;
      v=RuleGet(line,"buy_words"); if(v!="") g_buy=v;
      v=RuleGet(line,"sell_words"); if(v!="") g_sell=v;
      v=RuleGet(line,"limit_words"); if(v!="") g_limit=v;
      v=RuleGet(line,"tp_open_words"); if(v!="") g_tp_open=v;
      v=RuleGet(line,"open_tp_rr"); if(v!="") g_rr=StringToDouble(v);
      v=RuleGet(line,"compact_side_buy"); if(v!="") g_cbuy=v;
      v=RuleGet(line,"compact_side_sell"); if(v!="") g_csell=v;
      v=RuleGet(line,"compact_sl_word"); if(v!="") g_csl=v;
      v=RuleGet(line,"compact_tp_word"); if(v!="") g_ctp=v;
      v=RuleGet(line,"manage_enabled"); if(v!="") { string vu=v; StringToLower(vu); g_manage=(vu=="yes"||vu=="1"||vu=="true"||vu=="да"||vu=="on"); }
      v=RuleGet(line,"manage_link_hours"); if(v!="") g_link_hours=(int)StringToInteger(v);
      v=RuleGet(line,"manage_words_cancel"); if(v!="") g_m_cancel=v;
      v=RuleGet(line,"manage_words_reverse"); if(v!="") g_m_reverse=v;
      v=RuleGet(line,"manage_words_be"); if(v!="") g_m_be=v;
      v=RuleGet(line,"manage_words_close"); if(v!="") g_m_close=v;
      v=RuleGet(line,"manage_words_modify"); if(v!="") g_m_modify=v;
      v=RuleGet(line,"manage_words_market"); if(v!="") g_m_market=v;
      v=RuleGet(line,"manage_words_inherit"); if(v!="") g_m_inherit=v;
      v=RuleGet(line,"manage_words_levels"); if(v!="") g_m_levels=v;
      v=RuleGet(line,"manage_words_add"); if(v!="") g_m_add=v;
      v=RuleGet(line,"manage_words_keep"); if(v!="") g_m_keep=v;
     }
   FileClose(fh);
   PrintFormat("Rules loaded: channel=%s format=%s rr=%.2f manage=%s link=%dh",
               g_channel,g_format,g_rr,(g_manage?"ON":"off"),g_link_hours);
  }

string HtmlUnescape(string s)
  {
   StringReplace(s,"&amp;","&"); StringReplace(s,"&lt;","<"); StringReplace(s,"&gt;",">");
   StringReplace(s,"&quot;","\""); StringReplace(s,"&#39;","'"); StringReplace(s,"&nbsp;"," ");
   StringReplace(s,"<br>","\n"); StringReplace(s,"<br/>","\n"); StringReplace(s,"<br />","\n");
   while(true)
     { int a=StringFind(s,"<"); if(a<0) break; int b=StringFind(s,">",a); if(b<0) break;
       s=StringSubstr(s,0,a)+StringSubstr(s,b+1); }
   return s;
  }

string ExtractAttr(const string block,const string attr)
  {
   string key=attr+"=\""; int a=StringFind(block,key); if(a<0) return "";
   a+=StringLen(key); int b=StringFind(block,"\"",a); if(b<0) return "";
   return StringSubstr(block,a,b-a);
  }

datetime ParseIsoDate(string iso)
  {
   StringReplace(iso,"T"," ");
   int plus=StringFind(iso,"+"); int z=StringFind(iso,"Z");
   if(z>0) iso=StringSubstr(iso,0,z); else if(plus>10) iso=StringSubstr(iso,0,plus);
   if(StringLen(iso)>19){ string t=StringSubstr(iso,19,1); if(t=="+"||t=="-") iso=StringSubstr(iso,0,19); }
   StringTrimLeft(iso); StringTrimRight(iso); StringReplace(iso,"-",".");
   return StringToTime(iso);
  }

bool FetchChannelHtml(string &html)
  {
   html="";
   string url="https://t.me/s/"+g_channel;
   char post[]; ArrayResize(post,0); char result[]; string hdrs;
   string req=
      "User-Agent: Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/124.0.0.0\r\n"
      "Accept: text/html\r\nAccept-Language: ru,en;q=0.8\r\nAccept-Encoding: identity\r\n";
   ResetLastError();
   int code=WebRequest("GET",url,req,20000,post,result,hdrs);
   if(code==-1)
     { int err=GetLastError(); g_http_err++;
       if(g_http_err==1 || g_http_err%30==0)
          PrintFormat("WebRequest fail err=%d — добавьте https://t.me в список URL",err);
       return false; }
   if(code!=200){ g_http_err++; return false; }
   html=CharArrayToString(result,0,WHOLE_ARRAY,CP_UTF8);
   if(StringLen(html)<100) html=CharArrayToString(result,0,WHOLE_ARRAY,CP_ACP);
   return StringLen(html)>100;
  }

double ParsePriceToken(string s)
  {
   StringTrimLeft(s); StringTrimRight(s); StringReplace(s," ",""); StringReplace(s,"\xA0","");
   int c=StringFind(s,","), d=StringFind(s,".");
   if(c>=0 && d>=0){ if(c>d){ StringReplace(s,".",""); StringReplace(s,",","."); } else StringReplace(s,",",""); }
   else if(c>=0) StringReplace(s,",",".");
   return StringToDouble(s);
  }

bool HasPipeWord(const string hay_up,const string pipe_words)
  {
   if(pipe_words=="") return false;
   string parts[]; int n=StringSplit(pipe_words,'|',parts);
   for(int i=0;i<n;i++)
     { StringTrimLeft(parts[i]); StringTrimRight(parts[i]); StringToUpper(parts[i]);
       if(parts[i]!="" && StringFind(hay_up,parts[i])>=0) return true; }
   return false;
  }

bool HasAllMust(const string hay_up,const string pipe_words)
  {
   if(pipe_words=="") return true;
   string parts[]; int n=StringSplit(pipe_words,'|',parts);
   for(int i=0;i<n;i++)
     { StringTrimLeft(parts[i]); StringTrimRight(parts[i]); StringToUpper(parts[i]);
       if(parts[i]!="" && StringFind(hay_up,parts[i])<0) return false; }
   return true;
  }

string ExtractAfterLabel(const string text,const string label)
  {
   if(label=="") return "";
   string up=text; StringToUpper(up);
   string lab=label; StringToUpper(lab);
   int p=StringFind(up,lab); if(p<0) return "";
   string rest=StringSubstr(text,p+StringLen(label));
   StringTrimLeft(rest);
   if(StringLen(rest)>0 && StringGetCharacter(rest,0)==':') rest=StringSubstr(rest,1);
   StringTrimLeft(rest);
   int nl=StringFind(rest,"\n"); if(nl>=0) rest=StringSubstr(rest,0,nl);
   return rest;
  }

string NormalizeText(string t)
  {
   StringReplace(t,"\r","\n"); StringReplace(t,"▪️"," ");
   StringReplace(t,ShortToString((ushort)160)," ");
   // опечатки
   string up=t; StringToUpper(up);
   int p=StringFind(up,"СТОТ ЛОСС");
   if(p>=0) t=StringSubstr(t,0,p)+"стоп лосс"+StringSubstr(t,p+9);
   return t;
  }

double FirstPriceIn(string s)
  {
   StringTrimLeft(s); StringTrimRight(s);
   // пропуск нецифровых префиксов
   for(int i=0;i<StringLen(s);i++)
     {
      ushort c=StringGetCharacter(s,i);
      if((c>='0'&&c<='9')) return ParsePriceToken(StringSubstr(s,i));
     }
   return 0;
  }

double ExtractEntryPrice(const string t,const string up)
  {
   double v=FirstPriceIn(ExtractAfterLabel(t,g_lab_entry));
   if(v>0)
     {
      string raw=ExtractAfterLabel(t,g_lab_entry); string ru=raw; StringToUpper(ru);
      if(StringFind(ru,"НЕ МЕНЯЕМ")<0 && StringFind(ru,"БЕЗ ИЗМЕНЕН")<0) return v;
     }
   int pe=StringFind(up,"ТЕКУЩАЯ ЦЕНА"); if(pe>=0) { v=FirstPriceIn(StringSubstr(up,pe+12)); if(v>0) return v; }
   pe=StringFind(up,"МЕНЯЕМ ЦЕНУ ОТКРЫТИЯ"); if(pe>=0) { v=FirstPriceIn(StringSubstr(up,pe+20)); if(v>0) return v; }
   pe=StringFind(up,"ЦЕНУ ОТКРЫТИЯ НА"); if(pe>=0) { v=FirstPriceIn(StringSubstr(up,pe+15)); if(v>0) return v; }
   pe=StringFind(up,"ЦЕНА ОТКРЫТИЯ"); if(pe>=0) { v=FirstPriceIn(StringSubstr(up,pe+13)); if(v>0) return v; }
   pe=StringFind(up,"ОТКРЫТИЯ"); if(pe>=0) { v=FirstPriceIn(StringSubstr(up,pe+8)); if(v>0) return v; }
   return 0;
  }

double ExtractSlPrice(const string t,const string up)
  {
   // не брать число после «не меняем»
   int ps=StringFind(up,"СТОП ЛОСС НЕ МЕНЯ"); if(ps>=0) return 0;
   ps=StringFind(up,"СТОП ЛОСС ОБРАТНО");
   if(ps>=0) { double v=FirstPriceIn(StringSubstr(up,ps+16)); if(v>0) return v; }
   ps=StringFind(up,"ВЫСТАВЛЯЕМ СТОП ЛОСС");
   if(ps>=0) { double v=FirstPriceIn(StringSubstr(up,ps+20)); if(v>0) return v; }
   ps=StringFind(up,"СТОП ЛОСС");
   if(ps>=0)
     {
      string after=StringSubstr(up,ps+9); StringTrimLeft(after);
      if(StringFind(after,"НЕ МЕНЯ")==0) return 0;
      double v=FirstPriceIn(after); if(v>0) return v;
     }
   string raw=ExtractAfterLabel(t,g_lab_sl);
   if(raw!="")
     {
      string ru=raw; StringToUpper(ru);
      if(StringFind(ru,"НЕ МЕНЯЕМ")>=0 || StringFind(ru,"БЕЗ ИЗМЕНЕН")>=0) return 0;
      // число сразу после метки
      return FirstPriceIn(raw);
     }
   return 0;
  }

bool ExtractTpPrice(const string t,const string up,double &tp,bool &tp_open)
  {
   tp=0; tp_open=false;
   string tpraw=ExtractAfterLabel(t,g_lab_tp);
   string tpu=tpraw; StringToUpper(tpu);
   if(tpraw!="")
     {
      if(HasPipeWord(tpu,g_tp_open) || StringFind(tpu,"НЕ МЕНЯЕМ")>=0) { tp_open=true; return true; }
      tp=FirstPriceIn(tpraw); if(tp>0) return true;
     }
   int pt=StringFind(up,"ТЕЙК ПРОФИТ");
   if(pt>=0)
     {
      string after=StringSubstr(up,pt+11); StringTrimLeft(after);
      if(StringFind(after,"ОТКРЫТ")==0 || StringFind(after,"ОТКРЫТЫЙ")==0) { tp_open=true; return true; }
      tp=FirstPriceIn(after); if(tp>0) return true;
     }
   if(StringFind(up,"ОТКРЫТЫЙ ТЕЙК")>=0 || StringFind(up,"ТЕЙК ПРОФИТ ОСТАВЛЯЕМ ОТКРЫТ")>=0 ||
      StringFind(up,"ЦЕЛИ НЕ МЕНЯ")>=0) { tp_open=true; return true; }
   tp_open=true;
   return true;
  }

int DetectSide(const string t,const string up,const string sideblob)
  {
   string sbu=sideblob; StringToUpper(sbu);
   // 1) метка «на продажу / на покупку» важнее комментария
   if(sbu!="")
     {
      if(StringFind(sbu,"НА ПРОДАЖ")>=0 || StringFind(sbu,"ПРОДАЖА ПО РЫНК")>=0) return -1;
      if(StringFind(sbu,"НА ПОКУП")>=0 || StringFind(sbu,"ПОКУПКА ПО РЫНК")>=0) return 1;
      string head=StringSubstr(sbu,0,(StringLen(sbu)<160?StringLen(sbu):160));
      int par=StringFind(head,"("); if(par>0) head=StringSubstr(head,0,par);
      if(HasPipeWord(head,g_sell)) return -1;
      if(HasPipeWord(head,g_buy)) return 1;
     }
   // 2) разворот
   if(StringFind(up,"ЗАКРЫВА")>=0 && StringFind(up,"ПРОДАЖ")>=0 &&
      (StringFind(up,"ЛОНГ")>=0 || StringFind(up,"ПОКУП")>=0)) return 1;
   if(StringFind(up,"ЗАКРЫВА")>=0 && StringFind(up,"ПОКУП")>=0 &&
      (StringFind(up,"ШОРТ")>=0 || StringFind(up,"ПРОДАЖ")>=0)) return -1;
   // 3) действие входа в начале текста
   string headt=StringSubstr(up,0,(StringLen(up)<280?StringLen(up):280));
   if(StringFind(headt,"В ПОКУПКУ")>=0 || StringFind(headt,"В ЛОНГ")>=0 ||
      StringFind(headt,"ОТКРЫВАЕМ ЛОНГ")>=0 || StringFind(headt,"ЗАХОДИМ В ПОКУП")>=0) return 1;
   if(StringFind(headt,"В ПРОДАЖУ")>=0 || StringFind(headt,"В ШОРТ")>=0 ||
      StringFind(headt,"ОТКРЫВАЕМ ШОРТ")>=0 || StringFind(headt,"ЗАХОДИМ В ПРОДАЖ")>=0) return -1;
   if(HasPipeWord(headt,g_buy) && !HasPipeWord(headt,g_sell)) return 1;
   if(HasPipeWord(headt,g_sell) && !HasPipeWord(headt,g_buy)) return -1;
   return 0;
  }

int DetectAction(const string up,const bool formal)
  {
   if(formal) return 0; // open
   if(HasPipeWord(up,g_m_close)) return 1;
   // BE раньше reverse, но после close
   if(HasPipeWord(up,g_m_be) || StringFind(up,"НА ТОЧКУ ВХОДА")>=0 || StringFind(up,"ТОЧКУ ВХОДА")>=0)
     {
      // «у точки входа, закрываем» уже поймано close
      return 2;
     }
   if(HasPipeWord(up,g_m_reverse) ||
      (StringFind(up,"ЗАКРЫВА")>=0 && (StringFind(up,"ЗАХОДИМ")>=0 || StringFind(up,"ОТКРЫВАЕМ")>=0)))
      return 6;
   if(HasPipeWord(up,g_m_levels) || StringFind(up,"МЕНЯЕМ ЦЕНУ")>=0) return 4;
   if(HasPipeWord(up,g_m_cancel) && (HasPipeWord(up,g_m_market) || HasPipeWord(up,g_m_inherit) || StringFind(up,"ЦЕНА ОТКРЫТИЯ")>=0))
      return 5;
   if(HasPipeWord(up,g_m_cancel)) return 7;
   if(HasPipeWord(up,g_m_modify)) return 3;
   if(HasPipeWord(up,g_m_market) && (HasPipeWord(up,g_m_inherit) || StringFind(up,"ЦЕНА ОТКРЫТИЯ")>=0 || StringFind(up,"ОТКРЫВА")>=0))
      return 5;
   if(HasPipeWord(up,g_m_market) || StringFind(up,"ЦЕНА ОТКРЫТИЯ")>=0 || StringFind(up,"СТОП ЛОСС")>=0)
      return 5; // replace_or_open → в Exec разберём
   return 0;
  }

bool ParseSignalText(const string text_in,SSig &sig)
  {
   string t=NormalizeText(text_in);
   string up=t; StringToUpper(up);
   sig.text=t; sig.inherit=false; sig.action=0; sig.tp_open=false;

   bool formal=(g_lab_side!="" && StringFind(up,g_lab_side)>=0);
   bool manage_hit=g_manage && (
      HasPipeWord(up,g_m_cancel)||HasPipeWord(up,g_m_reverse)||HasPipeWord(up,g_m_modify)||
      HasPipeWord(up,g_m_close)||HasPipeWord(up,g_m_be)||HasPipeWord(up,g_m_market)||
      HasPipeWord(up,g_m_inherit)||HasPipeWord(up,g_m_levels)||StringFind(up,"ЦЕНА ОТКРЫТИЯ")>=0||
      StringFind(up,"ТЕКУЩАЯ ЦЕНА")>=0);

   if(!HasAllMust(up,g_must))
     {
      if(!(g_manage && (manage_hit || StringFind(t,"#")>=0)))
         return false;
     }
   if(HasPipeWord(up,g_skip))
     {
      if(!g_manage && !formal) return false;
     }

   // symbol #
   int hash=StringFind(t,"#"); if(hash<0 && g_format!="compact") return false;
   string sym="";
   if(hash>=0)
     {
      string tag=StringSubstr(t,hash+1);
      for(int k=0;k<StringLen(tag) && k<12;k++)
        { ushort c=StringGetCharacter(tag,k);
          if((c>='A'&&c<='Z')||(c>='a'&&c<='z')||(c>='0'&&c<='9')) sym=sym+ShortToString(c); else break; }
      StringToUpper(sym);
     }

   int side=0, is_limit=0; double entry=0, sl=0, tp=0; bool tp_open=false;
   bool inherit=HasPipeWord(up,g_m_inherit) || StringFind(up,"НЕ МЕНЯЕМ")>=0 || StringFind(up,"БЕЗ ИЗМЕНЕНИЙ")>=0;

   if(g_format=="compact")
     {
      string cb=g_cbuy; StringToUpper(cb); string cs=g_csell; StringToUpper(cs);
      int pb=StringFind(up," "+cb+" "); if(pb<0) pb=StringFind(up,cb+" ");
      int ps=StringFind(up," "+cs+" "); if(ps<0) ps=StringFind(up,cs+" ");
      int side_pos=-1;
      if(pb>=0 && (ps<0 || pb<=ps)){ side=1; side_pos=pb; }
      else if(ps>=0){ side=-1; side_pos=ps; } else return false;
      string after=StringSubstr(up,side_pos); StringTrimLeft(after);
      if(StringFind(after,cb)==0) after=StringSubstr(after,StringLen(cb));
      else if(StringFind(after,cs)==0) after=StringSubstr(after,StringLen(cs));
      StringTrimLeft(after); entry=ParsePriceToken(after);
      string csl=g_csl; StringToUpper(csl); string ctp=g_ctp; StringToUpper(ctp);
      int psl=StringFind(up,csl); int ptp=StringFind(up,ctp);
      if(psl<0 || ptp<0) return false;
      sl=ParsePriceToken(StringSubstr(up,psl+StringLen(csl)));
      tp=ParsePriceToken(StringSubstr(up,ptp+StringLen(ctp)));
     }
   else
     {
      string sideblob=ExtractAfterLabel(t,g_lab_side);
      string sbu=sideblob; StringToUpper(sbu);
      if(sideblob=="") sbu=up;
      if(HasPipeWord(sbu,g_limit) || (sideblob=="" && StringFind(up,"ЛИМИТ")>=0)) is_limit=1;
      side=DetectSide(t,up,sideblob);

      entry=ExtractEntryPrice(t,up);
      sl=ExtractSlPrice(t,up);
      ExtractTpPrice(t,up,tp,tp_open);

      if(HasPipeWord(up,g_m_cancel) || HasPipeWord(up,g_m_market) || StringFind(up,"РЫНК")>=0)
         is_limit=0;
      else if(StringFind(up,"ЛИМИТ")>=0 && StringFind(up,"УДАЛЯ")<0) is_limit=1;
     }

   // сторона из геометрии
   if(side==0 && entry>0 && sl>0)
     {
      if(sl<entry) side=1;
      else if(sl>entry) side=-1;
     }

   if(formal && entry>0 && sl>0) inherit=false;
   if(inherit && entry>0 && sl>0 && MathAbs(sl-entry)<1e-9) sl=0;

   sig.action=DetectAction(up,formal);
   sig.inherit=inherit;

   // manage-only: close/be/modify без полного набора
   if(StringLen(sym)>=3 && (sig.action==1 || sig.action==2 || sig.action==3 || sig.action==7))
     {
      sig.symbol=sym; sig.side=side; sig.is_limit=is_limit;
      sig.entry=entry; sig.sl=sl; sig.tp=tp; sig.tp_open=tp_open;
      return true;
     }

   // inherit: вход есть, SL возьмём из цепочки в Exec
   if(StringLen(sym)>=3 && inherit && (side!=0 || entry>0) && sl<=0 &&
      (entry>0 || StringFind(up,"РЫНК")>=0))
     {
      if(side==0) side=1; // временно; Exec подтянет из позиции
      sig.symbol=sym; sig.side=side; sig.is_limit=0;
      sig.entry=entry; sig.sl=0; sig.tp=tp; sig.tp_open=true;
      if(sig.action==0) sig.action=5;
      return true;
     }

   // modify_levels
   if(StringLen(sym)>=3 && sig.action==4 && entry>0)
     {
      sig.symbol=sym; sig.side=side; sig.is_limit=is_limit;
      sig.entry=entry; sig.sl=sl; sig.tp=tp; sig.tp_open=tp_open;
      return true;
     }

   if(StringLen(sym)<3 || side==0 || entry<=0 || sl<=0) return false;
   if(side>0 && !(sl<entry)) return false;
   if(side<0 && !(sl>entry)) return false;
   if(tp_open || tp<=0)
     { double risk=MathAbs(entry-sl); if(g_rr<=0 || risk<=0) return false;
       tp=(side>0)? entry+g_rr*risk : entry-g_rr*risk; tp_open=true; }

   sig.symbol=sym; sig.side=side; sig.is_limit=is_limit;
   sig.entry=entry; sig.sl=sl; sig.tp=tp; sig.tp_open=tp_open;
   return true;
  }

long MaxPostId(const string html)
  {
   string marker="data-post=\"", prefix=g_channel+"/";
   long mx=0; int pos=0;
   while(true)
     { int a=StringFind(html,marker,pos); if(a<0) break; a+=StringLen(marker);
       int b=StringFind(html,"\"",a); if(b<0) break;
       string post=StringSubstr(html,a,b-a); pos=b+1;
       if(StringFind(post,prefix)!=0) continue;
       int slash=StringFind(post,"/"); if(slash<0) continue;
       long mid=StringToInteger(StringSubstr(post,slash+1)); if(mid>mx) mx=mid; }
   return mx;
  }

int ParsePageSignals(const string html,SSig &out[])
  {
   ArrayResize(out,0);
   string marker="data-post=\"", prefix=g_channel+"/";
   int pos=0, seen=0, with=0, parsed=0;
   while(true)
     {
      int a=StringFind(html,marker,pos); if(a<0) break;
      int block_start=a; a+=StringLen(marker);
      int b=StringFind(html,"\"",a); if(b<0) break;
      string post=StringSubstr(html,a,b-a); pos=b+1; seen++;
      if(StringFind(post,prefix)!=0) continue;
      int slash=StringFind(post,"/"); if(slash<0) continue;
      long mid=StringToInteger(StringSubstr(post,slash+1)); if(mid<=0) continue;
      int next=StringFind(html,marker,pos);
      int blen=(next<0)? (int)MathMin(16000,StringLen(html)-block_start) : next-block_start;
      if(blen<50) continue;
      string block=StringSubstr(html,block_start,blen);
      datetime when=ParseIsoDate(ExtractAttr(block,"datetime"));
      string text=""; int ti=StringFind(block,"tgme_widget_message_text");
      if(ti>=0){ int gt=StringFind(block,">",ti); if(gt>=0){
         int take=(int)MathMin(5000,StringLen(block)-(gt+1));
         if(take>0) text=StringSubstr(block,gt+1,take); }}
      StringReplace(text,ShortToString((ushort)160)," ");
      text=HtmlUnescape(text);
      string up=text; StringToUpper(up);
      // кандидаты: must / стоп / рынок / inherit / #symbol с manage
      bool cand=HasAllMust(up,g_must) || StringFind(up,"SL")>=0 || StringFind(up,"СТОП")>=0 ||
                StringFind(up,"РЫНК")>=0 || StringFind(up,"ЦЕНА ОТКРЫТИЯ")>=0 ||
                StringFind(up,"ТЕКУЩАЯ ЦЕНА")>=0 || StringFind(up,"МЕНЯЕМ ЦЕН")>=0 ||
                (g_manage && StringFind(text,"#")>=0);
      if(!cand) continue;
      with++;
      SSig sig; ZeroMemory(sig);
      sig.msg_id=mid; sig.time_utc=when; sig.text=text;
      if(!ParseSignalText(text,sig))
        { if(InpVerboseParse) PrintFormat("fail #%I64d %s",mid,StringSubstr(text,0,120)); continue; }
      parsed++; int n=ArraySize(out); ArrayResize(out,n+1); out[n]=sig;
      if(InpVerboseParse)
         PrintFormat("ok #%I64d %s %s act=%d E=%.5f SL=%.5f inh=%d",
                     mid,sig.symbol,(sig.side>0?"BUY":"SELL"),sig.action,sig.entry,sig.sl,(int)sig.inherit);
     }
   PrintFormat("Parse stats: posts=%d candidates=%d parsed=%d",seen,with,parsed);
   return ArraySize(out);
  }

bool SelectFill(const string sym)
  {
   long m=SymbolInfoInteger(sym,SYMBOL_FILLING_MODE);
   if((m&SYMBOL_FILLING_IOC)==SYMBOL_FILLING_IOC){ g_trade.SetTypeFilling(ORDER_FILLING_IOC); return true; }
   if((m&SYMBOL_FILLING_FOK)==SYMBOL_FILLING_FOK){ g_trade.SetTypeFilling(ORDER_FILLING_FOK); return true; }
   g_trade.SetTypeFilling(ORDER_FILLING_RETURN); return true;
  }
double NormPx(const string sym,double px){ return NormalizeDouble(px,(int)SymbolInfoInteger(sym,SYMBOL_DIGITS)); }
double NormLot(const string sym,double lot)
  {
   double vmin=SymbolInfoDouble(sym,SYMBOL_VOLUME_MIN), vmax=SymbolInfoDouble(sym,SYMBOL_VOLUME_MAX), step=SymbolInfoDouble(sym,SYMBOL_VOLUME_STEP);
   if(step<=0) step=0.01; if(vmin<=0) vmin=step;
   lot=MathFloor(lot/step+1e-12)*step; if(lot<vmin) lot=vmin; if(lot>vmax) lot=vmax;
   return NormalizeDouble(lot,(step<0.01-1e-12)?3:2);
  }
double LotByRisk(const string sym,double price,double sl)
  {
   if(InpRiskPercent<=0) return NormLot(sym,InpFixedLot);
   double risk_money=AccountInfoDouble(ACCOUNT_EQUITY)*InpRiskPercent/100.0;
   double tick_size=SymbolInfoDouble(sym,SYMBOL_TRADE_TICK_SIZE);
   double tick_val=SymbolInfoDouble(sym,SYMBOL_TRADE_TICK_VALUE);
   double dist=MathAbs(price-sl);
   if(tick_size<=0||tick_val<=0||dist<=0) return NormLot(sym,InpFixedLot);
   return NormLot(sym, risk_money/(dist/tick_size*tick_val));
  }
bool AdjustStops(const string sym,int side,double price,double &sl,double &tp)
  {
   double point=SymbolInfoDouble(sym,SYMBOL_POINT); if(point<=0) point=_Point;
   int stops=(int)SymbolInfoInteger(sym,SYMBOL_TRADE_STOPS_LEVEL);
   double min_dist=(stops+InpMaxStopAdjustPts)*point; if(min_dist<point) min_dist=point;
   if(side>0){ if(!(sl<price && tp>price)) return false;
     if(price-sl<min_dist) sl=NormPx(sym,price-min_dist);
     if(tp-price<min_dist) tp=NormPx(sym,price+min_dist); return (sl<price && tp>price); }
   if(!(sl>price && tp<price)) return false;
   if(sl-price<min_dist) sl=NormPx(sym,price+min_dist);
   if(price-tp<min_dist) tp=NormPx(sym,price-min_dist); return (sl>price && tp<price);
  }
bool Already(const string sym,long msg_id)
  {
   string needle=InpCommentPrefix+IntegerToString(msg_id);
   for(int i=PositionsTotal()-1;i>=0;i--)
     if(g_pos.SelectByIndex(i) && g_pos.Magic()==InpMagic && StringFind(g_pos.Comment(),needle)>=0) return true;
   for(int i=OrdersTotal()-1;i>=0;i--)
     if(g_ord.SelectByIndex(i) && g_ord.Magic()==InpMagic && StringFind(g_ord.Comment(),needle)>=0) return true;
   return false;
  }

bool FindMagicPos(const string sym,ulong &ticket,double &vol)
  {
   ticket=0; vol=0;
   for(int i=PositionsTotal()-1;i>=0;i--)
     {
      if(!g_pos.SelectByIndex(i)) continue;
      if(g_pos.Magic()!=InpMagic) continue;
      if(BaseKey(g_pos.Symbol())!=BaseKey(sym) && g_pos.Symbol()!=sym) continue;
      ticket=g_pos.Ticket(); vol=g_pos.Volume(); return true;
     }
   return false;
  }

bool CancelPendingBySymbol(const string sym)
  {
   bool any=false;
   for(int i=OrdersTotal()-1;i>=0;i--)
     {
      if(!g_ord.SelectByIndex(i)) continue;
      if(g_ord.Magic()!=InpMagic) continue;
      if(BaseKey(g_ord.Symbol())!=BaseKey(sym) && g_ord.Symbol()!=sym) continue;
      if(InpDryRun){ PrintFormat("DRY cancel pending #%I64u",g_ord.Ticket()); any=true; continue; }
      if(g_trade.OrderDelete(g_ord.Ticket())) any=true;
     }
   return any;
  }

bool CloseMagicPos(const string sym)
  {
   ulong ticket; double vol;
   if(!FindMagicPos(sym,ticket,vol)) return false;
   if(InpDryRun){ PrintFormat("DRY close %s ticket=%I64u",sym,ticket); return true; }
   return g_trade.PositionClose(ticket);
  }

bool ModifyMagicSL(const string sym,double new_sl,double new_tp)
  {
   ulong ticket; double vol;
   if(!FindMagicPos(sym,ticket,vol)) return false;
   if(!g_pos.SelectByTicket(ticket)) return false;
   double sl=NormPx(sym,new_sl);
   double tp=(new_tp>0)? NormPx(sym,new_tp):g_pos.TakeProfit();
   if(InpDryRun){ PrintFormat("DRY modify SL %s -> %.5f",sym,sl); return true; }
   return g_trade.PositionModify(ticket,sl,tp);
  }

bool FindPendingBySymbol(const string sym,ulong &ticket,double &price,double &sl,double &tp)
  {
   ticket=0; price=0; sl=0; tp=0;
   for(int i=OrdersTotal()-1;i>=0;i--)
     {
      if(!g_ord.SelectByIndex(i)) continue;
      if(g_ord.Magic()!=InpMagic) continue;
      if(BaseKey(g_ord.Symbol())!=BaseKey(sym) && g_ord.Symbol()!=sym) continue;
      ticket=g_ord.Ticket(); price=g_ord.PriceOpen(); sl=g_ord.StopLoss(); tp=g_ord.TakeProfit();
      return true;
     }
   return false;
  }

bool FillFromChain(SSig &s,const string sym)
  {
   ulong ticket; double vol, px, psl, ptp;
   if(FindMagicPos(sym,ticket,vol) && g_pos.SelectByTicket(ticket))
     {
      if(s.side==0)
         s.side=(g_pos.PositionType()==POSITION_TYPE_BUY)?1:-1;
      double old_e=g_pos.PriceOpen(), old_sl=g_pos.StopLoss();
      if(s.sl<=0) s.sl=old_sl;
      if(s.tp<=0) s.tp=g_pos.TakeProfit();
      if(s.entry<=0) s.entry=old_e;
      // геометрия
      if(s.entry>0 && s.sl>0)
        {
         bool ok=(s.side>0 && s.sl<s.entry) || (s.side<0 && s.sl>s.entry);
         if(!ok)
           {
            double risk=MathAbs(old_e-old_sl);
            if(risk<=0) risk=MathAbs(s.entry)*0.002;
            s.sl=(s.side>0)? s.entry-risk : s.entry+risk;
           }
        }
      if((s.tp_open || s.tp<=0) && s.entry>0 && s.sl>0 && g_rr>0)
         s.tp=(s.side>0)? s.entry+g_rr*MathAbs(s.entry-s.sl) : s.entry-g_rr*MathAbs(s.entry-s.sl);
      return true;
     }
   if(FindPendingBySymbol(sym,ticket,px,psl,ptp))
     {
      if(s.side==0)
        {
         // сторона по геометрии pending
         if(psl>0 && px>0) s.side=(psl<px)?1:-1;
        }
      if(s.sl<=0) s.sl=psl;
      if(s.tp<=0) s.tp=ptp;
      double old_e=px, old_sl=psl;
      if(s.entry<=0) s.entry=px;
      if(s.entry>0 && s.sl>0)
        {
         bool ok=(s.side>0 && s.sl<s.entry) || (s.side<0 && s.sl>s.entry);
         if(!ok)
           {
            double risk=MathAbs(old_e-old_sl);
            if(risk<=0) risk=MathAbs(s.entry)*0.002;
            s.sl=(s.side>0)? s.entry-risk : s.entry+risk;
           }
        }
      if((s.tp_open || s.tp<=0) && s.entry>0 && s.sl>0 && g_rr>0)
         s.tp=(s.side>0)? s.entry+g_rr*MathAbs(s.entry-s.sl) : s.entry-g_rr*MathAbs(s.entry-s.sl);
      return true;
     }
   return false;
  }

bool ModifyPendingLevels(const string sym,double entry,double sl,double tp)
  {
   ulong ticket; double px, psl, ptp;
   if(!FindPendingBySymbol(sym,ticket,px,psl,ptp)) return false;
   double ne=(entry>0)? NormPx(sym,entry):px;
   double ns=(sl>0)? NormPx(sym,sl):psl;
   double nt=(tp>0)? NormPx(sym,tp):ptp;
   if(InpDryRun){ PrintFormat("DRY modify pending #%I64u E=%.5f SL=%.5f",ticket,ne,ns); return true; }
   return g_trade.OrderModify(ticket,ne,ns,nt,ORDER_TIME_GTC,0);
  }

// true = полностью обработано (не открывать заново), false = нужно открытие
bool ApplyManage(SSig &s)
  {
   if(!g_manage) return false;
   string up=s.text; StringToUpper(up);
   string sym=Resolve(s.symbol);
   if(sym=="") return false;

   string lab=g_lab_side; StringToUpper(lab);
   bool formal=(lab!="" && StringFind(up,lab)>=0);
   if(formal && s.action==0 && !s.inherit) return false; // обычный новый сигнал

   ulong ticket; double vol;
   bool has_pos=FindMagicPos(sym,ticket,vol);
   bool has_pend=false; ulong pt; double pp,psl,ptp; has_pend=FindPendingBySymbol(sym,pt,pp,psl,ptp);
   bool busy=has_pos || has_pend;

   int act=s.action;
   // уточнение replace vs reverse при занятой цепочке
   if(busy && (act==0 || act==5) && has_pos && g_pos.SelectByTicket(ticket))
     {
      long pos_side=(g_pos.PositionType()==POSITION_TYPE_BUY)?1:-1;
      if(s.side!=0 && pos_side!=s.side) act=6;
      else if(s.inherit || HasPipeWord(up,g_m_cancel) || HasPipeWord(up,g_m_market)) act=5;
     }

   if(act==1) // close
     {
      if(has_pos) CloseMagicPos(sym);
      CancelPendingBySymbol(sym);
      PrintFormat("MANAGE close #%I64d %s",s.msg_id,sym);
      return true;
     }
   if(act==2) // breakeven
     {
      if(has_pos && g_pos.SelectByTicket(ticket))
        {
         double nsl=g_pos.PriceOpen();
         ModifyMagicSL(sym,nsl,g_pos.TakeProfit());
         PrintFormat("MANAGE BE #%I64d %s SL->entry %.5f",s.msg_id,sym,nsl);
         return true;
        }
      return true; // нечего двигать
     }
   if(act==3) // modify_sl
     {
      double nsl=s.sl;
      if(nsl<=0 && has_pos && g_pos.SelectByTicket(ticket)) nsl=g_pos.PriceOpen();
      if(has_pos && nsl>0){ ModifyMagicSL(sym,nsl,s.tp); PrintFormat("MANAGE SL #%I64d %s -> %.5f",s.msg_id,sym,nsl); return true; }
      if(has_pend && nsl>0){ ModifyPendingLevels(sym,0,nsl,s.tp); return true; }
      return true;
     }
   if(act==4) // modify_levels
     {
      if(!FillFromChain(s,sym) && s.sl<=0) return true;
      if(has_pend)
        {
         ModifyPendingLevels(sym,s.entry,s.sl,s.tp);
         PrintFormat("MANAGE levels pending #%I64d %s E=%.5f SL=%.5f",s.msg_id,sym,s.entry,s.sl);
         return true;
        }
      if(has_pos && s.sl>0)
        {
         ModifyMagicSL(sym,s.sl,s.tp);
         return true;
        }
      // нет цепочки — открыть как новый лимит
      s.is_limit=1;
      return false;
     }
   if(act==7) // cancel pending only
     {
      CancelPendingBySymbol(sym);
      PrintFormat("MANAGE cancel #%I64d %s",s.msg_id,sym);
      return true;
     }
   if(act==6) // reverse
     {
      if(has_pos) CloseMagicPos(sym);
      CancelPendingBySymbol(sym);
      if(s.inherit || s.sl<=0) FillFromChain(s,sym);
      s.is_limit=0;
      PrintFormat("MANAGE reverse #%I64d %s -> open",s.msg_id,sym);
      return false; // open new side
     }
   if(act==5 || s.inherit) // replace_market / inherit open
     {
      if(!HasPipeWord(up,g_m_keep)) CancelPendingBySymbol(sym);
      if(s.inherit || s.sl<=0)
        {
         if(!FillFromChain(s,sym) && s.sl<=0)
           {
            PrintFormat("MANAGE inherit skip #%I64d %s — нет цепочки",s.msg_id,sym);
            return true;
           }
        }
      // если позиция того же направления — только SL
      if(has_pos && g_pos.SelectByTicket(ticket))
        {
         long pos_side=(g_pos.PositionType()==POSITION_TYPE_BUY)?1:-1;
         if(s.side==0) s.side=(int)pos_side;
         if(pos_side==s.side && s.sl>0 && s.entry<=0)
           { ModifyMagicSL(sym,s.sl,s.tp); return true; }
         if(pos_side!=s.side){ CloseMagicPos(sym); }
         else if(HasPipeWord(up,g_m_cancel) || HasPipeWord(up,g_m_market))
           { /* замена: закроем и откроем заново по рынку */ CloseMagicPos(sym); }
         else if(s.sl>0){ ModifyMagicSL(sym,s.sl,s.tp); return true; }
        }
      s.is_limit=0;
      PrintFormat("MANAGE replace_market #%I64d %s E=%.5f SL=%.5f",s.msg_id,sym,s.entry,s.sl);
      return false; // open market
     }

   // занятая цепочка + неформальный апдейт с уровнями
   if(busy && !formal)
     {
      if(s.inherit || s.sl<=0) FillFromChain(s,sym);
      if(has_pos && s.sl>0 && s.is_limit!=0)
        { ModifyMagicSL(sym,s.sl,s.tp); }
      if(has_pend && s.is_limit==0)
        { CancelPendingBySymbol(sym); return false; }
      if(has_pos && s.is_limit==0 && s.side!=0)
        {
         if(g_pos.SelectByTicket(ticket))
           {
            long pos_side=(g_pos.PositionType()==POSITION_TYPE_BUY)?1:-1;
            if(pos_side!=s.side){ CloseMagicPos(sym); return false; }
            if(s.sl>0){ ModifyMagicSL(sym,s.sl,s.tp); return true; }
           }
        }
     }
   return false;
  }

bool ExecSig(SSig &s)
  {
   if(IsDone(s.msg_id)) return true;
   if(!InOnly(s.symbol)){ MarkDone(s.msg_id); g_skipped++; return true; }
   if(InpMaxSignalAgeSec>0 && s.time_utc>0)
     { long age=(long)(TimeGMT()-s.time_utc);
       if(age>InpMaxSignalAgeSec){ MarkDone(s.msg_id); g_skipped++; return true; } }
   string sym=Resolve(s.symbol);
   if(sym==""){ PrintFormat("SKIP %I64d %s: no symbol",s.msg_id,s.symbol); MarkDone(s.msg_id); g_skipped++; return true; }
   if(Already(sym,s.msg_id)){ MarkDone(s.msg_id); return true; }

   // сопровождение цепочки
   if(g_manage)
     {
      bool done=ApplyManage(s);
      if(done){ MarkDone(s.msg_id); g_opened++; return true; }
      // после manage мог подтянуться SL/side
      if(s.inherit && s.sl<=0)
        {
         if(!FillFromChain(s,sym))
           { PrintFormat("SKIP inherit #%I64d %s — нет позиции/лимита",s.msg_id,sym);
             MarkDone(s.msg_id); g_skipped++; return true; }
        }
     }

   // рынок без цены — текущий ask/bid
   MqlTick tk; if(!SymbolInfoTick(sym,tk)||tk.ask<=0){ return false; }
   if(s.entry<=0 && s.is_limit==0)
      s.entry=(s.side>0)? tk.ask:tk.bid;
   if(s.side==0 || s.sl<=0)
     { MarkDone(s.msg_id); g_skipped++; return true; }
   if(s.tp<=0 && g_rr>0)
     { double risk=MathAbs(s.entry-s.sl);
       s.tp=(s.side>0)? s.entry+g_rr*risk : s.entry-g_rr*risk; }

   bool use_limit=(s.is_limit!=0 && InpAllowLimit);
   double price=use_limit? NormPx(sym,s.entry):(s.side>0?tk.ask:tk.bid);
   double sl=NormPx(sym,s.sl), tp=NormPx(sym,s.tp);
   double ref=use_limit? price:(s.side>0?tk.ask:tk.bid);
   if(InpSkipBadDirection)
     { if(s.side>0 && !(sl<ref && tp>ref)){ MarkDone(s.msg_id); g_skipped++; return true; }
       if(s.side<0 && !(sl>ref && tp<ref)){ MarkDone(s.msg_id); g_skipped++; return true; } }
   if(!AdjustStops(sym,s.side,ref,sl,tp)){ MarkDone(s.msg_id); g_skipped++; return true; }
   double lot=LotByRisk(sym,ref,sl);
   string cmt=InpCommentPrefix+IntegerToString(s.msg_id); if(StringLen(cmt)>31) cmt=StringSubstr(cmt,0,31);
   PrintFormat("SIGNAL #%I64d %s %s E=%.5f SL=%.5f TP=%.5f act=%d",
               s.msg_id,sym,(s.side>0?"BUY":"SELL"),s.entry,s.sl,s.tp,s.action);
   if(InpDryRun){ PrintFormat("DRY-RUN lot=%.2f %s",lot,cmt); MarkDone(s.msg_id); g_opened++; return true; }
   SelectFill(sym); bool ok=false;
   if(use_limit) ok=(s.side>0)? g_trade.BuyLimit(lot,price,sym,sl,tp,ORDER_TIME_GTC,0,cmt)
                              : g_trade.SellLimit(lot,price,sym,sl,tp,ORDER_TIME_GTC,0,cmt);
   else ok=(s.side>0)? g_trade.Buy(lot,sym,price,sl,tp,cmt):g_trade.Sell(lot,sym,price,sl,tp,cmt);
   if(!ok){ PrintFormat("FAIL %s",g_trade.ResultRetcodeDescription()); g_failed++;
     uint rc=g_trade.ResultRetcode();
     if(rc==TRADE_RETCODE_INVALID_STOPS||rc==TRADE_RETCODE_NO_MONEY){ MarkDone(s.msg_id); return true; }
     return false; }
   MarkDone(s.msg_id); g_opened++;
   PrintFormat("OPEN #%d %s %s lot=%.2f",g_opened,(s.side>0?"BUY":"SELL"),sym,lot);
   return true;
  }

void PollTelegram()
  {
   string html; if(!FetchChannelHtml(html)) return; g_polls++;
   SSig sigs[]; int n=ParsePageSignals(html,sigs);
   if(!g_bootstrapped)
     {
      g_seen_max_id=MaxPostId(html);
      for(int i=0;i<n;i++) if(sigs[i].msg_id>g_seen_max_id) g_seen_max_id=sigs[i].msg_id;
      g_bootstrapped=true;
      if(InpIgnoreHistory)
        { for(int i=0;i<n;i++) MarkDone(sigs[i].msg_id);
          PrintFormat("Bootstrap OK max_id=%I64d parsed=%d — ждём НОВЫЕ посты",g_seen_max_id,n);
          return; }
     }
   for(int i=0;i<n;i++)
     {
      if(InpIgnoreHistory && sigs[i].msg_id<=g_seen_max_id){ MarkDone(sigs[i].msg_id); continue; }
      if(IsDone(sigs[i].msg_id)) continue;
      ExecSig(sigs[i]);
      if(sigs[i].msg_id>g_seen_max_id) g_seen_max_id=sigs[i].msg_id;
     }
  }

int OnInit()
  {
   g_trade.SetExpertMagicNumber(InpMagic);
   g_trade.SetDeviationInPoints(InpSlippagePoints);
   LoadDone(); LoadRulesFile();
   Print("=== SignalKit Live EA v1.20 (chains+inherit) ===");
   PrintFormat("Channel t.me/s/%s | DryRun=%s | lot=%.2f | rr=%.2f | manage=%s",
               g_channel,(InpDryRun?"YES":"no"),InpFixedLot,g_rr,(g_manage?"ON":"off"));
   EventSetTimer(MathMax(5,InpPollSeconds));
   PollTelegram();
   return INIT_SUCCEEDED;
  }
void OnDeinit(const int r){ EventKillTimer(); PrintFormat("opened=%d skipped=%d failed=%d",g_opened,g_skipped,g_failed); }
void OnTimer(){ PollTelegram(); }
void OnTick(){}
//+------------------------------------------------------------------+

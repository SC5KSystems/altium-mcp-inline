{..............................................................................}
{ Board editing: the board outline, the origin, and free primitives.           }
{                                                                              }
{ Coordinates cross this boundary as mils relative to the board origin, the    }
{ same convention as the placement tools; they are converted to absolute       }
{ Altium coordinates here. Geometry is built and validated on the Python side  }
{ (server/board_geometry.py), which hands over plain pipe-delimited spec       }
{ files, so this unit only applies numbers it is given.                        }
{                                                                              }
{ Every write refuses to run unless a PCB is the active document, and when the }
{ caller names an expected board, unless the active board's file name          }
{ contains it. The focus helper otherwise falls back to the first PCB in the   }
{ focused project, which may not be the board the caller meant.               }
{                                                                              }
{ Enum values are written as integers where this codebase has not yet proven   }
{ the named constant: an unknown identifier parks the script in the debugger. }
{   TPolySegmentType: 0 = line, 1 = arc                                         }
{   TPolyHatchStyle:  5 = solid                                                 }
{..............................................................................}

// Progress log for these commands. The bridge deletes the file before each
// command and, if Altium never answers, reports its last line. Altium parks a
// failed script in its debugger with no dialog, so this is the only way to
// learn which statement failed.
procedure BoardEditLog(Msg: String);
var
    StepLines : TStringList;
    StepPath  : String;
begin
    StepPath := 'C:\Users\Public\altium_mcp\steps.txt';
    StepLines := TStringList.Create;
    try
        if FileExists(StepPath) then
            StepLines.LoadFromFile(StepPath);
        StepLines.Add(Msg);
        StepLines.SaveToFile(StepPath);
    finally
        StepLines.Free;
    end;
end;

// '' when Board may be edited, otherwise the reason it may not.
function BoardEditCheck(Board: IPCB_Board; ExpectedBoard: String): String;
begin
    Result := '';
    if Board = nil then
    begin
        Result := 'No PCB document is active - open the board, click its tab, and retry';
        Exit;
    end;
    if Trim(ExpectedBoard) <> '' then
        if Pos(LowerCase(Trim(ExpectedBoard)), LowerCase(ExtractFileName(Board.FileName))) = 0 then
            Result := 'The active board is ' + ExtractFileName(Board.FileName) +
                      ', not the expected "' + Trim(ExpectedBoard) + '". Nothing was changed.';
end;

// Layer by name, or eNoLayer. Tries Altium's own name table (with the usual
// hyphen/space spellings), then copper layers by the name shown in the layer
// stack, then enabled mechanical layers by custom name or "Mechanical N".
function ResolveBoardLayer(Board: IPCB_Board; LayerName: String): TLayer;
var
    TheLayerStack : IPCB_LayerStack_V7;
    LayerObj      : IPCB_LayerObject;
    MechLayer     : IPCB_MechanicalLayer;
    Wanted        : String;
    j             : Integer;
begin
    Result := eNoLayer;
    Wanted := Trim(LayerName);
    if Wanted = '' then Exit;

    Result := String2Layer(Wanted);
    if Result <> eNoLayer then Exit;
    Result := String2Layer(StringReplace(Wanted, '-', ' ', REPLACEALL));
    if Result <> eNoLayer then Exit;
    Result := String2Layer(StringReplace(Wanted, ' ', '-', REPLACEALL));
    if Result <> eNoLayer then Exit;
    Result := String2Layer(StringReplace(Wanted, ' ', '', REPLACEALL));
    if Result <> eNoLayer then Exit;

    TheLayerStack := Board.LayerStack_V7;
    if TheLayerStack = nil then Exit;

    LayerObj := TheLayerStack.FirstLayer;
    while (LayerObj <> nil) do
    begin
        if LowerCase(LayerObj.Name) = LowerCase(Wanted) then
        begin
            Result := LayerObj.V6_LayerID;
            Exit;
        end;
        LayerObj := TheLayerStack.NextLayer(LayerObj);
    end;

    for j := 1 to 32 do
    begin
        MechLayer := TheLayerStack.LayerObject_V7[ILayer.MechanicalLayer(j)];
        if MechLayer.MechanicalLayerEnabled then
        begin
            if (LowerCase(MechLayer.Name) = LowerCase(Wanted)) or
               (LowerCase(Wanted) = 'mechanical ' + IntToStr(j)) then
            begin
                Result := ILayer.MechanicalLayer(j);
                Exit;
            end;
        end;
    end;
end;

// Board nets named in Wanted, matched case-insensitively, returned as two
// parallel lists: the name in Names and the net object in Nets at the same
// index. One pass over the board's nets however many primitives use them.
procedure CollectBoardNets(Board: IPCB_Board; Wanted: TStringList; Names: TStringList; Nets: TObjectList);
var
    Iterator : IPCB_BoardIterator;
    Net      : IPCB_Net;
begin
    if Wanted.Count = 0 then Exit;
    Iterator := Board.BoardIterator_Create;
    Iterator.AddFilter_ObjectSet(MkSet(eNetObject));
    Iterator.AddFilter_LayerSet(AllLayers);
    Iterator.AddFilter_Method(eProcessAll);
    Net := Iterator.FirstPCBObject;
    while (Net <> nil) do
    begin
        if (Wanted.IndexOf(Net.Name) >= 0) and (Names.IndexOf(Net.Name) < 0) then
        begin
            Names.Add(Net.Name);
            Nets.Add(Net);
        end;
        Net := Iterator.NextPCBObject;
    end;
    Board.BoardIterator_Destroy(Iterator);
end;

// The outline as a JSON array of segments (mils relative to the origin), plus
// its point count and Altium's own bounding rectangle as a cross-check on the
// arc geometry computed in Python.
procedure AddOutlineJSON(Board: IPCB_Board; Props: TStringList; ArrayName: String);
var
    Outline : IPCB_Primitive;
    Segs    : TStringList;
    SProps  : TStringList;
    XO, YO  : TCoord;
    i, N    : Integer;
    ArcSeen : Boolean;
begin
    Outline := Board.BoardOutline;
    XO := Board.XOrigin;
    YO := Board.YOrigin;
    N := Outline.PointCount;
    ArcSeen := False;

    AddJSONInteger(Props, ArrayName + '_point_count', N);
    AddJSONNumber(Props, ArrayName + '_bbox_left', CoordToMils(Outline.BoundingRectangle.Left - XO));
    AddJSONNumber(Props, ArrayName + '_bbox_bottom', CoordToMils(Outline.BoundingRectangle.Bottom - YO));
    AddJSONNumber(Props, ArrayName + '_bbox_right', CoordToMils(Outline.BoundingRectangle.Right - XO));
    AddJSONNumber(Props, ArrayName + '_bbox_top', CoordToMils(Outline.BoundingRectangle.Top - YO));

    Segs := TStringList.Create;
    try
        for i := 0 to N - 1 do
        begin
            SProps := TStringList.Create;
            try
                AddJSONInteger(SProps, 'kind', Outline.Segments[i].Kind);
                AddJSONNumber(SProps, 'x', CoordToMils(Outline.Segments[i].vx - XO));
                AddJSONNumber(SProps, 'y', CoordToMils(Outline.Segments[i].vy - YO));
                if Outline.Segments[i].Kind = 1 then
                begin
                    if not ArcSeen then
                    begin
                        BoardEditLog('reading arc fields cx, cy, Radius, Angle1, Angle2 of ' +
                                     ArrayName + ' segment ' + IntToStr(i));
                        ArcSeen := True;
                    end;
                    AddJSONNumber(SProps, 'cx', CoordToMils(Outline.Segments[i].cx - XO));
                    AddJSONNumber(SProps, 'cy', CoordToMils(Outline.Segments[i].cy - YO));
                    AddJSONNumber(SProps, 'radius', CoordToMils(Outline.Segments[i].Radius));
                    AddJSONNumber(SProps, 'start_angle', Outline.Segments[i].Angle1);
                    AddJSONNumber(SProps, 'end_angle', Outline.Segments[i].Angle2);
                end;
                Segs.Add(BuildJSONObject(SProps));
            finally
                SProps.Free;
            end;
        end;
        Props.Add(Trim(BuildJSONArray(Segs, ArrayName)));
    finally
        Segs.Free;
    end;
end;

// Free regions that are not plain copper: board cutouts, polygon cutouts and
// the like, with their kind number as Altium stores it.
procedure AddFreeRegionsJSON(Board: IPCB_Board; Props: TStringList);
var
    Iterator : IPCB_BoardIterator;
    Prim     : IPCB_Primitive;
    Regions  : TStringList;
    RProps   : TStringList;
    Verts    : TStringList;
    VProps   : TStringList;
    XO, YO   : TCoord;
    V        : Integer;
begin
    XO := Board.XOrigin;
    YO := Board.YOrigin;
    Regions := TStringList.Create;
    try
        Iterator := Board.BoardIterator_Create;
        Iterator.AddFilter_ObjectSet(MkSet(eRegionObject));
        Iterator.AddFilter_LayerSet(AllLayers);
        Iterator.AddFilter_Method(eProcessAll);
        Prim := Iterator.FirstPCBObject;
        while (Prim <> nil) do
        begin
            if (not Prim.InComponent) and (Prim.Kind <> 0) then
            begin
                RProps := TStringList.Create;
                Verts := TStringList.Create;
                try
                    AddJSONInteger(RProps, 'kind', Prim.Kind);
                    AddJSONProperty(RProps, 'layer', Board.LayerName(Prim.Layer));
                    AddJSONNumber(RProps, 'bbox_left', CoordToMils(Prim.BoundingRectangle.Left - XO));
                    AddJSONNumber(RProps, 'bbox_bottom', CoordToMils(Prim.BoundingRectangle.Bottom - YO));
                    AddJSONNumber(RProps, 'bbox_right', CoordToMils(Prim.BoundingRectangle.Right - XO));
                    AddJSONNumber(RProps, 'bbox_top', CoordToMils(Prim.BoundingRectangle.Top - YO));
                    AddJSONInteger(RProps, 'vertex_count', Prim.MainContour.Count);
                    for V := 1 to Prim.MainContour.Count do
                    begin
                        if V <= 200 then
                        begin
                            VProps := TStringList.Create;
                            try
                                AddJSONNumber(VProps, 'x', CoordToMils(Prim.MainContour.x[V] - XO));
                                AddJSONNumber(VProps, 'y', CoordToMils(Prim.MainContour.y[V] - YO));
                                Verts.Add(BuildJSONObject(VProps));
                            finally
                                VProps.Free;
                            end;
                        end;
                    end;
                    RProps.Add(Trim(BuildJSONArray(Verts, 'vertices')));
                    Regions.Add(BuildJSONObject(RProps));
                finally
                    Verts.Free;
                    RProps.Free;
                end;
            end;
            Prim := Iterator.NextPCBObject;
        end;
        Board.BoardIterator_Destroy(Iterator);
        Props.Add(Trim(BuildJSONArray(Regions, 'non_copper_regions')));
    finally
        Regions.Free;
    end;
end;

// Every component's bounding box (designator text excluded), so the caller
// can tell which parts a new outline leaves outside or on the edge.
procedure AddComponentBoxesJSON(Board: IPCB_Board; Props: TStringList);
var
    Iterator  : IPCB_BoardIterator;
    Component : IPCB_Component;
    Rect      : TCoordRect;
    Boxes     : TStringList;
    CProps    : TStringList;
    XO, YO    : TCoord;
begin
    XO := Board.XOrigin;
    YO := Board.YOrigin;
    Boxes := TStringList.Create;
    try
        Iterator := Board.BoardIterator_Create;
        Iterator.AddFilter_ObjectSet(MkSet(eComponentObject));
        Iterator.AddFilter_IPCB_LayerSet(LayerSet.AllLayers);
        Iterator.AddFilter_Method(eProcessAll);
        Component := Iterator.FirstPCBObject;
        while (Component <> nil) do
        begin
            Rect := Component.BoundingRectangleNoNameComment;
            CProps := TStringList.Create;
            try
                AddJSONProperty(CProps, 'designator', Component.Name.Text);
                AddJSONProperty(CProps, 'layer', Layer2String(Component.Layer));
                AddJSONNumber(CProps, 'left', CoordToMils(Rect.Left - XO));
                AddJSONNumber(CProps, 'bottom', CoordToMils(Rect.Bottom - YO));
                AddJSONNumber(CProps, 'right', CoordToMils(Rect.Right - XO));
                AddJSONNumber(CProps, 'top', CoordToMils(Rect.Top - YO));
                Boxes.Add(BuildJSONObject(CProps));
            finally
                CProps.Free;
            end;
            Component := Iterator.NextPCBObject;
        end;
        Board.BoardIterator_Destroy(Iterator);
        Props.Add(Trim(BuildJSONArray(Boxes, 'components')));
    finally
        Boxes.Free;
    end;
end;

{..............................................................................}
{ get_board_outline                                                            }
{..............................................................................}
function GetBoardOutlineData(ROOT_DIR: String; IncludeComponents: Boolean): String;
var
    Board       : IPCB_Board;
    Props       : TStringList;
    OutputLines : TStringList;
    Rebuilt     : Boolean;
begin
    BoardEditLog('get_board_outline: start');
    Board := GetBoardSafe(0);
    if Board = nil then
    begin
        Result := 'ERROR: No PCB document is active - open the board, click its tab, and retry';
        Exit;
    end;

    Props := TStringList.Create;
    try
        AddJSONProperty(Props, 'board_file', ExtractFileName(Board.FileName));
        AddJSONNumber(Props, 'origin_x_absolute_mils', CoordToMils(Board.XOrigin));
        AddJSONNumber(Props, 'origin_y_absolute_mils', CoordToMils(Board.YOrigin));

        // Altium's own sample rebuilds the outline before reading it. Only do
        // that when it reads as empty, so a plain read changes nothing.
        Rebuilt := False;
        BoardEditLog('reading Board.BoardOutline.PointCount');
        if Board.BoardOutline.PointCount < 2 then
        begin
            BoardEditLog('outline reads empty - Invalidate, Rebuild, Validate');
            Board.BoardOutline.Invalidate;
            Board.BoardOutline.Rebuild;
            Board.BoardOutline.Validate;
            Rebuilt := True;
        end;
        AddJSONBoolean(Props, 'outline_rebuilt_before_read', Rebuilt);

        BoardEditLog('reading outline segments');
        AddOutlineJSON(Board, Props, 'outline');
        BoardEditLog('reading non-copper regions');
        AddFreeRegionsJSON(Board, Props);
        if IncludeComponents then
        begin
            BoardEditLog('reading component bounding boxes');
            AddComponentBoxesJSON(Board, Props);
        end;
        BoardEditLog('get_board_outline: done');

        OutputLines := TStringList.Create;
        try
            OutputLines.Text := BuildJSONObject(Props);
            Result := WriteJSONToFile(OutputLines, ROOT_DIR + 'temp_board_outline.json');
        finally
            OutputLines.Free;
        end;
    finally
        Props.Free;
    end;
end;

{..............................................................................}
{ set_board_outline                                                            }
{                                                                              }
{ Spec lines, mils relative to the origin, one per segment:                    }
{   SEG|LINE|x|y                                                               }
{   SEG|ARC|x|y|cx|cy|radius|start_angle|end_angle                             }
{ (x, y) is where the segment starts; it runs to the next segment's vertex.    }
{ Written the way Altium's polygon sample builds a polygon: SetState_PointCount }
{ with N, then SetState_Segments for 0..N where N repeats segment 0 to close.  }
{..............................................................................}
function SetBoardOutline(ROOT_DIR: String; SpecPath: String; ExpectedBoard: String): String;
var
    Board       : IPCB_Board;
    Outline     : IPCB_Primitive;
    Lines       : TStringList;
    SegLines    : TStringList;
    Props       : TStringList;
    OutputLines : TStringList;
    Seg         : TPolySegment;
    Line, Kind  : String;
    ErrorMsg    : String;
    XO, YO      : TCoord;
    i, N        : Integer;
    HasArcs     : Boolean;
begin
    BoardEditLog('set_board_outline: start');
    Board := GetBoardSafe(0);
    ErrorMsg := BoardEditCheck(Board, ExpectedBoard);
    if ErrorMsg <> '' then
    begin
        Result := 'ERROR: ' + ErrorMsg;
        Exit;
    end;
    if not FileExists(SpecPath) then
    begin
        Result := 'ERROR: Outline spec file not found: ' + SpecPath;
        Exit;
    end;

    Lines := TStringList.Create;
    SegLines := TStringList.Create;
    Props := TStringList.Create;
    try
        Lines.LoadFromFile(SpecPath);
        HasArcs := False;
        for i := 0 to Lines.Count - 1 do
        begin
            Line := Trim(Lines[i]);
            if UpperCase(GetFieldFromPipeString(Line, 0)) = 'SEG' then
            begin
                Kind := UpperCase(GetFieldFromPipeString(Line, 1));
                if (Kind <> 'LINE') and (Kind <> 'ARC') then
                begin
                    Result := 'ERROR: Outline spec line ' + IntToStr(i + 1) +
                              ' has unknown segment kind "' + Kind + '". Nothing was changed.';
                    Exit;
                end;
                if Kind = 'ARC' then HasArcs := True;
                SegLines.Add(Line);
            end;
        end;
        N := SegLines.Count;
        if N < 2 then
        begin
            Result := 'ERROR: The outline spec has ' + IntToStr(N) +
                      ' segments; at least 2 are needed. Nothing was changed.';
            Exit;
        end;

        XO := Board.XOrigin;
        YO := Board.YOrigin;
        Outline := Board.BoardOutline;

        AddJSONProperty(Props, 'board_file', ExtractFileName(Board.FileName));
        AddJSONNumber(Props, 'origin_x_absolute_mils', CoordToMils(XO));
        AddJSONNumber(Props, 'origin_y_absolute_mils', CoordToMils(YO));

        // What is about to be replaced, so the change can be undone by value.
        BoardEditLog('reading the current outline');
        AddOutlineJSON(Board, Props, 'previous');

        // Fill throwaway segments first: a wrong field name then fails here,
        // before anything on the board has been touched.
        BoardEditLog('probe: new TPolySegment with line fields Kind, vx, vy');
        Seg := TPolySegment;
        Seg.Kind := 0;
        Seg.vx := XO;
        Seg.vy := YO;
        if HasArcs then
        begin
            BoardEditLog('probe: new TPolySegment with arc fields cx, cy, Radius, Angle1, Angle2');
            Seg := TPolySegment;
            Seg.Kind := 1;
            Seg.vx := XO;
            Seg.vy := YO;
            Seg.cx := XO;
            Seg.cy := YO;
            Seg.Radius := MilsToCoord(100);
            Seg.Angle1 := 0;
            Seg.Angle2 := 90;
        end;

        BoardEditLog('PreProcess and BeginModify on the outline');
        PCBServer.PreProcess;
        PCBServer.SendMessageToRobots(Outline.I_ObjectAddress, c_Broadcast, PCBM_BeginModify, c_NoEventData);

        BoardEditLog('Outline.SetState_PointCount(' + IntToStr(N) + ')');
        Outline.SetState_PointCount(N);
        for i := 0 to N do
        begin
            if i < N then
                Line := SegLines[i]
            else
                Line := SegLines[0];
            Seg := TPolySegment;
            if UpperCase(GetFieldFromPipeString(Line, 1)) = 'ARC' then
            begin
                Seg.Kind := 1;
                Seg.vx := XO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 2)));
                Seg.vy := YO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 3)));
                Seg.cx := XO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 4)));
                Seg.cy := YO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 5)));
                Seg.Radius := MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 6)));
                Seg.Angle1 := SafeStrToFloat(GetFieldFromPipeString(Line, 7));
                Seg.Angle2 := SafeStrToFloat(GetFieldFromPipeString(Line, 8));
            end
            else
            begin
                Seg.Kind := 0;
                Seg.vx := XO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 2)));
                Seg.vy := YO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 3)));
            end;
            if i = 0 then
                BoardEditLog('Outline.SetState_Segments(0, ...)');
            Outline.SetState_Segments(i, Seg);
        end;

        BoardEditLog('EndModify, then Invalidate / Rebuild / Validate');
        PCBServer.SendMessageToRobots(Outline.I_ObjectAddress, c_Broadcast, PCBM_EndModify, c_NoEventData);
        Outline.Invalidate;
        Outline.Rebuild;
        Outline.Validate;
        PCBServer.PostProcess;

        BoardEditLog('refreshing the view');
        Board.ViewManager_FullUpdate;
        Client.SendMessage('PCB:Zoom', 'Action=Redraw', 255, Client.CurrentView);

        BoardEditLog('reading the outline back');
        AddOutlineJSON(Board, Props, 'outline');
        BoardEditLog('reading component bounding boxes');
        AddComponentBoxesJSON(Board, Props);
        BoardEditLog('set_board_outline: done');

        OutputLines := TStringList.Create;
        try
            OutputLines.Text := BuildJSONObject(Props);
            Result := WriteJSONToFile(OutputLines, ROOT_DIR + 'temp_set_board_outline.json');
        finally
            OutputLines.Free;
        end;
    finally
        Props.Free;
        SegLines.Free;
        Lines.Free;
    end;
end;

{..............................................................................}
{ set_board_origin                                                             }
{   Mode lower_left / center: from the outline's bounding rectangle.           }
{   Mode point: RelX, RelY in mils relative to the CURRENT origin.             }
{..............................................................................}
function SetBoardOriginTo(ROOT_DIR: String; Mode: String; RelX, RelY: Double; ExpectedBoard: String): String;
var
    Board       : IPCB_Board;
    Props       : TStringList;
    OutputLines : TStringList;
    ErrorMsg    : String;
    OldX, OldY  : TCoord;
    NewX, NewY  : TCoord;
    L, B, R, T  : TCoord;
begin
    BoardEditLog('set_board_origin: start');
    Board := GetBoardSafe(0);
    ErrorMsg := BoardEditCheck(Board, ExpectedBoard);
    if ErrorMsg <> '' then
    begin
        Result := 'ERROR: ' + ErrorMsg;
        Exit;
    end;

    OldX := Board.XOrigin;
    OldY := Board.YOrigin;
    Mode := LowerCase(Trim(Mode));
    if (Mode = 'lower_left') or (Mode = 'center') then
    begin
        BoardEditLog('reading the outline bounding rectangle');
        L := Board.BoardOutline.BoundingRectangle.Left;
        B := Board.BoardOutline.BoundingRectangle.Bottom;
        R := Board.BoardOutline.BoundingRectangle.Right;
        T := Board.BoardOutline.BoundingRectangle.Top;
        if Mode = 'lower_left' then
        begin
            NewX := L;
            NewY := B;
        end
        else
        begin
            NewX := Round((L + R) / 2);
            NewY := Round((B + T) / 2);
        end;
    end
    else if Mode = 'point' then
    begin
        NewX := OldX + MilsToCoord(RelX);
        NewY := OldY + MilsToCoord(RelY);
    end
    else
    begin
        Result := 'ERROR: Unknown origin position "' + Mode + '". Nothing was changed.';
        Exit;
    end;

    BoardEditLog('assigning Board.XOrigin and Board.YOrigin');
    PCBServer.PreProcess;
    Board.XOrigin := NewX;
    Board.YOrigin := NewY;
    PCBServer.PostProcess;

    BoardEditLog('refreshing the view');
    Board.ViewManager_FullUpdate;
    Client.SendMessage('PCB:Zoom', 'Action=Redraw', 255, Client.CurrentView);

    Props := TStringList.Create;
    try
        AddJSONProperty(Props, 'board_file', ExtractFileName(Board.FileName));
        AddJSONProperty(Props, 'position', Mode);
        AddJSONNumber(Props, 'old_origin_x_absolute_mils', CoordToMils(OldX));
        AddJSONNumber(Props, 'old_origin_y_absolute_mils', CoordToMils(OldY));
        AddJSONNumber(Props, 'requested_origin_x_absolute_mils', CoordToMils(NewX));
        AddJSONNumber(Props, 'requested_origin_y_absolute_mils', CoordToMils(NewY));
        // Read back rather than assume the assignment took.
        AddJSONNumber(Props, 'origin_x_absolute_mils', CoordToMils(Board.XOrigin));
        AddJSONNumber(Props, 'origin_y_absolute_mils', CoordToMils(Board.YOrigin));
        AddJSONNumber(Props, 'moved_by_x_mils', CoordToMils(Board.XOrigin - OldX));
        AddJSONNumber(Props, 'moved_by_y_mils', CoordToMils(Board.YOrigin - OldY));
        BoardEditLog('set_board_origin: done');

        OutputLines := TStringList.Create;
        try
            OutputLines.Text := BuildJSONObject(Props);
            Result := WriteJSONToFile(OutputLines, ROOT_DIR + 'temp_set_board_origin.json');
        finally
            OutputLines.Free;
        end;
    finally
        Props.Free;
    end;
end;

{..............................................................................}
{ create_pcb_primitives                                                        }
{                                                                              }
{ Spec lines, mils relative to the origin (fields after the kind):             }
{   TRACK|layer|net|x1|y1|x2|y2|width                                          }
{   ARC|layer|net|cx|cy|radius|start_angle|end_angle|width                     }
{   VIA|net|x|y|diameter|hole|low_layer|high_layer                             }
{   FILL|layer|net|x1|y1|x2|y2|rotation                                        }
{   REGION|layer|net|kind|n|x1|y1|...|xn|yn                                    }
{   PAD|layer|net|x|y|size_x|size_y|hole|shape|plated|rotation|name            }
{   TEXT|layer|x|y|height|stroke_width|rotation|mirror|text                    }
{   POLYGON|layer|net|pour|n|x1|y1|...|xn|yn                                   }
{ The whole batch is checked first - every layer and net must resolve - and    }
{ nothing is created unless all of it can be. Creation is one undo step.       }
{..............................................................................}

// The net field of a spec line, '' when the kind carries none.
function SpecNetName(Kind: String; Line: String): String;
begin
    Result := '';
    if Kind = 'VIA' then
        Result := Trim(GetFieldFromPipeString(Line, 1))
    else if Kind <> 'TEXT' then
        Result := Trim(GetFieldFromPipeString(Line, 2));
end;

function CreateBoardPrimitives(ROOT_DIR: String; SpecPath: String; ExpectedBoard: String): String;
var
    Board        : IPCB_Board;
    Prim         : IPCB_Primitive;
    Net          : IPCB_Net;
    Contour      : IPCB_Contour;
    Seg          : TPolySegment;
    Lines        : TStringList;
    Errors       : TStringList;
    WantedNets   : TStringList;
    NetNames     : TStringList;
    NetObjs      : TObjectList;
    Logged       : TStringList;
    Props        : TStringList;
    OutputLines  : TStringList;
    Line, Kind   : String;
    NetName      : String;
    Shape        : String;
    ErrorMsg     : String;
    XO, YO       : TCoord;
    Hole         : Double;
    i, k, V, NPts, Idx : Integer;
    NTrack, NArc, NVia, NFill, NRegion, NPad, NText, NPoly, NPoured : Integer;
begin
    BoardEditLog('create_pcb_primitives: start');
    Board := GetBoardSafe(0);
    ErrorMsg := BoardEditCheck(Board, ExpectedBoard);
    if ErrorMsg <> '' then
    begin
        Result := 'ERROR: ' + ErrorMsg;
        Exit;
    end;
    if not FileExists(SpecPath) then
    begin
        Result := 'ERROR: Primitive spec file not found: ' + SpecPath;
        Exit;
    end;

    Lines := TStringList.Create;
    Errors := TStringList.Create;
    WantedNets := TStringList.Create;
    NetNames := TStringList.Create;
    NetObjs := CreateObject(TObjectList);
    NetObjs.OwnsObjects := False;
    Logged := TStringList.Create;
    Props := TStringList.Create;
    try
        WantedNets.CaseSensitive := False;
        NetNames.CaseSensitive := False;
        Lines.LoadFromFile(SpecPath);

        // Pass 1: resolve every layer and net before touching the board.
        BoardEditLog('checking layers and nets for ' + IntToStr(Lines.Count) + ' spec lines');
        for i := 0 to Lines.Count - 1 do
        begin
            Line := Lines[i];
            Kind := UpperCase(Trim(GetFieldFromPipeString(Line, 0)));
            if Kind = '' then Continue;
            if (Kind <> 'TRACK') and (Kind <> 'ARC') and (Kind <> 'VIA') and (Kind <> 'FILL') and
               (Kind <> 'REGION') and (Kind <> 'PAD') and (Kind <> 'TEXT') and (Kind <> 'POLYGON') then
            begin
                Errors.Add('line ' + IntToStr(i + 1) + ': unknown kind ' + Kind);
                Continue;
            end;
            if Kind = 'VIA' then
            begin
                if ResolveBoardLayer(Board, GetFieldFromPipeString(Line, 6)) = eNoLayer then
                    Errors.Add('line ' + IntToStr(i + 1) + ': unknown layer "' + GetFieldFromPipeString(Line, 6) + '"');
                if ResolveBoardLayer(Board, GetFieldFromPipeString(Line, 7)) = eNoLayer then
                    Errors.Add('line ' + IntToStr(i + 1) + ': unknown layer "' + GetFieldFromPipeString(Line, 7) + '"');
            end
            else if ResolveBoardLayer(Board, GetFieldFromPipeString(Line, 1)) = eNoLayer then
                Errors.Add('line ' + IntToStr(i + 1) + ': unknown layer "' + GetFieldFromPipeString(Line, 1) + '"');
            NetName := SpecNetName(Kind, Line);
            if (NetName <> '') and (WantedNets.IndexOf(NetName) < 0) then
                WantedNets.Add(NetName);
        end;

        BoardEditLog('resolving ' + IntToStr(WantedNets.Count) + ' nets');
        CollectBoardNets(Board, WantedNets, NetNames, NetObjs);
        for i := 0 to WantedNets.Count - 1 do
            if NetNames.IndexOf(WantedNets[i]) < 0 then
                Errors.Add('no net named "' + WantedNets[i] + '" on this board');

        if Errors.Count > 0 then
        begin
            ErrorMsg := '';
            for i := 0 to Errors.Count - 1 do
                if i < 20 then
                    ErrorMsg := ErrorMsg + Errors[i] + '; ';
            if Errors.Count > 20 then
                ErrorMsg := ErrorMsg + '(' + IntToStr(Errors.Count - 20) + ' more) ';
            Result := 'ERROR: Nothing was created. ' + ErrorMsg;
            Exit;
        end;

        // Pass 2: create everything as one undo step.
        XO := Board.XOrigin;
        YO := Board.YOrigin;
        NTrack := 0; NArc := 0; NVia := 0; NFill := 0; NRegion := 0;
        NPad := 0; NText := 0; NPoly := 0; NPoured := 0;

        BoardEditLog('PreProcess');
        PCBServer.PreProcess;
        for i := 0 to Lines.Count - 1 do
        begin
            Line := Lines[i];
            Kind := UpperCase(Trim(GetFieldFromPipeString(Line, 0)));
            if Kind = '' then Continue;
            if Logged.IndexOf(Kind) < 0 then
            begin
                Logged.Add(Kind);
                BoardEditLog('creating the first ' + Kind + ' (spec line ' + IntToStr(i + 1) + ')');
            end;

            Net := nil;
            NetName := SpecNetName(Kind, Line);
            if NetName <> '' then
            begin
                k := NetNames.IndexOf(NetName);
                if k >= 0 then Net := NetObjs[k];
            end;

            Prim := nil;
            if Kind = 'TRACK' then
            begin
                Prim := PCBServer.PCBObjectFactory(eTrackObject, eNoDimension, eCreate_Default);
                Prim.x1 := XO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 3)));
                Prim.y1 := YO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 4)));
                Prim.x2 := XO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 5)));
                Prim.y2 := YO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 6)));
                Prim.Width := MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 7)));
                Prim.Layer := ResolveBoardLayer(Board, GetFieldFromPipeString(Line, 1));
                Inc(NTrack);
            end
            else if Kind = 'ARC' then
            begin
                Prim := PCBServer.PCBObjectFactory(eArcObject, eNoDimension, eCreate_Default);
                Prim.XCenter := XO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 3)));
                Prim.YCenter := YO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 4)));
                Prim.Radius := MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 5)));
                Prim.StartAngle := SafeStrToFloat(GetFieldFromPipeString(Line, 6));
                Prim.EndAngle := SafeStrToFloat(GetFieldFromPipeString(Line, 7));
                Prim.LineWidth := MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 8)));
                Prim.Layer := ResolveBoardLayer(Board, GetFieldFromPipeString(Line, 1));
                Inc(NArc);
            end
            else if Kind = 'VIA' then
            begin
                Prim := PCBServer.PCBObjectFactory(eViaObject, eNoDimension, eCreate_Default);
                Prim.x := XO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 2)));
                Prim.y := YO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 3)));
                Prim.Size := MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 4)));
                Prim.HoleSize := MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 5)));
                Prim.LowLayer := ResolveBoardLayer(Board, GetFieldFromPipeString(Line, 6));
                Prim.HighLayer := ResolveBoardLayer(Board, GetFieldFromPipeString(Line, 7));
                Inc(NVia);
            end
            else if Kind = 'FILL' then
            begin
                Prim := PCBServer.PCBObjectFactory(eFillObject, eNoDimension, eCreate_Default);
                Prim.x1Location := XO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 3)));
                Prim.y1Location := YO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 4)));
                Prim.x2Location := XO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 5)));
                Prim.y2Location := YO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 6)));
                Prim.Rotation := SafeStrToFloat(GetFieldFromPipeString(Line, 7));
                Prim.Layer := ResolveBoardLayer(Board, GetFieldFromPipeString(Line, 1));
                Inc(NFill);
            end
            else if Kind = 'REGION' then
            begin
                Prim := PCBServer.PCBObjectFactory(eRegionObject, eNoDimension, eCreate_Default);
                Contour := PCBServer.PCBContourFactory;
                NPts := StrToInt(Trim(GetFieldFromPipeString(Line, 4)));
                for V := 0 to NPts - 1 do
                    Contour.AddPoint(XO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 5 + 2 * V))),
                                     YO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 6 + 2 * V))));
                Prim.SetOutlineContour(Contour);
                Prim.Layer := ResolveBoardLayer(Board, GetFieldFromPipeString(Line, 1));
                Prim.Kind := StrToInt(Trim(GetFieldFromPipeString(Line, 3)));
                Inc(NRegion);
            end
            else if Kind = 'PAD' then
            begin
                Prim := PCBServer.PCBObjectFactory(ePadObject, eNoDimension, eCreate_Default);
                Prim.Mode := ePadMode_Simple;
                Prim.x := XO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 3)));
                Prim.y := YO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 4)));
                Prim.Layer := ResolveBoardLayer(Board, GetFieldFromPipeString(Line, 1));
                Prim.TopXSize := MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 5)));
                Prim.TopYSize := MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 6)));
                Shape := LowerCase(Trim(GetFieldFromPipeString(Line, 8)));
                if Shape = 'rect' then
                    Prim.TopShape := eRectangular
                else if Shape = 'rounded_rect' then
                    Prim.TopShape := eRoundedRectangular
                else
                    Prim.TopShape := eRounded;
                Hole := SafeStrToFloat(GetFieldFromPipeString(Line, 7));
                Prim.HoleSize := MilsToCoord(Hole);
                if Hole > 0 then
                    Prim.HoleType := 0;
                Prim.Plated := (Trim(GetFieldFromPipeString(Line, 9)) = '1');
                Prim.Name := GetFieldFromPipeString(Line, 11);
                // Rotation last: it turns the pad about its location
                Prim.Rotation := SafeStrToFloat(GetFieldFromPipeString(Line, 10));
                Inc(NPad);
            end
            else if Kind = 'TEXT' then
            begin
                Prim := PCBServer.PCBObjectFactory(eTextObject, eNoDimension, eCreate_Default);
                Prim.XLocation := XO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 2)));
                Prim.YLocation := YO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 3)));
                Prim.Size := MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 4)));
                Prim.Width := MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 5)));
                Prim.Layer := ResolveBoardLayer(Board, GetFieldFromPipeString(Line, 1));
                Prim.MirrorFlag := (Trim(GetFieldFromPipeString(Line, 7)) = '1');
                Prim.UseTTFonts := False;
                Prim.Text := StringReplace(StringReplace(GetFieldFromPipeString(Line, 8),
                                 '<NL>', #13#10, REPLACEALL), '<PIPE>', '|', REPLACEALL);
                Prim.Rotation := SafeStrToFloat(GetFieldFromPipeString(Line, 6));
                Inc(NText);
            end
            else if Kind = 'POLYGON' then
            begin
                // Built the way Altium's polygon sample does it; the segment
                // list repeats the first vertex at index N to close the shape.
                Prim := PCBServer.PCBObjectFactory(ePolyObject, eNoDimension, eCreate_Default);
                NPts := StrToInt(Trim(GetFieldFromPipeString(Line, 4)));
                Prim.SetState_PointCount(NPts);
                for V := 0 to NPts do
                begin
                    if V < NPts then Idx := V else Idx := 0;
                    Seg := TPolySegment;
                    Seg.Kind := 0;
                    Seg.vx := XO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 5 + 2 * Idx)));
                    Seg.vy := YO + MilsToCoord(SafeStrToFloat(GetFieldFromPipeString(Line, 6 + 2 * Idx)));
                    Prim.SetState_Segments(V, Seg);
                end;
                Prim.SetState_PolyHatchStyle(5);
                Prim.Layer := ResolveBoardLayer(Board, GetFieldFromPipeString(Line, 1));
                Inc(NPoly);
            end;

            if Prim <> nil then
            begin
                if Net <> nil then
                    Prim.Net := Net;
                Board.AddPCBObject(Prim);
                PCBServer.SendMessageToRobots(Board.I_ObjectAddress, c_Broadcast, PCBM_BoardRegisteration, Prim.I_ObjectAddress);
                if (Kind = 'POLYGON') and (Trim(GetFieldFromPipeString(Line, 3)) = '1') then
                begin
                    if NPoured = 0 then
                        BoardEditLog('pouring the first polygon (Rebuild)');
                    Prim.Rebuild;
                    Inc(NPoured);
                end;
            end;
        end;
        BoardEditLog('PostProcess');
        PCBServer.PostProcess;

        BoardEditLog('refreshing the view');
        Board.ViewManager_FullUpdate;
        Client.SendMessage('PCB:Zoom', 'Action=Redraw', 255, Client.CurrentView);

        AddJSONProperty(Props, 'board_file', ExtractFileName(Board.FileName));
        AddJSONInteger(Props, 'tracks', NTrack);
        AddJSONInteger(Props, 'arcs', NArc);
        AddJSONInteger(Props, 'vias', NVia);
        AddJSONInteger(Props, 'fills', NFill);
        AddJSONInteger(Props, 'regions', NRegion);
        AddJSONInteger(Props, 'pads', NPad);
        AddJSONInteger(Props, 'texts', NText);
        AddJSONInteger(Props, 'polygons', NPoly);
        AddJSONInteger(Props, 'polygons_poured', NPoured);
        AddJSONInteger(Props, 'created', NTrack + NArc + NVia + NFill + NRegion + NPad + NText + NPoly);
        BoardEditLog('create_pcb_primitives: done');

        OutputLines := TStringList.Create;
        try
            OutputLines.Text := BuildJSONObject(Props);
            Result := WriteJSONToFile(OutputLines, ROOT_DIR + 'temp_create_primitives.json');
        finally
            OutputLines.Free;
        end;
    finally
        Props.Free;
        Logged.Free;
        NetObjs.Free;
        NetNames.Free;
        WantedNets.Free;
        Errors.Free;
        Lines.Free;
    end;
end;

{..............................................................................}
{ delete_pcb_primitives                                                        }
{                                                                              }
{ Two passes. The first only collects matches, so a failure there - or a dry   }
{ run - leaves the board untouched. The second removes them as one undo step,  }
{ and only when every match fits under MaxObjects.                             }
{                                                                              }
{ Never touched: anything inside a component (pads, silkscreen, designators),  }
{ copper a polygon poured (checked for tracks, arcs, fills and regions), and   }
{ the pieces of a dimension (checked for tracks, arcs and text).               }
{..............................................................................}
function DeleteBoardPrimitives(ROOT_DIR: String; TypesList: TStringList; LayerName: String;
                               NetFilter: TStringList; HasWithin: Boolean;
                               WX1, WY1, WX2, WY2: Double; SelectedOnly: Boolean;
                               DryRun: Boolean; MaxObjects: Integer; ExpectedBoard: String): String;
var
    Board        : IPCB_Board;
    Iterator     : IPCB_BoardIterator;
    Prim         : IPCB_Primitive;
    Candidates   : TObjectList;
    Report       : TStringList;
    EProps       : TStringList;
    Props        : TStringList;
    OutputLines  : TStringList;
    TypeName     : String;
    ErrorMsg     : String;
    PrimNet      : String;
    L            : TLayer;
    XO, YO       : TCoord;
    AX1, AY1, AX2, AY2 : TCoord;
    i, Matched, Deleted : Integer;
    Keep, CanPour, CanDim, LoggedPoly, LoggedDim : Boolean;
begin
    BoardEditLog('delete_pcb_primitives: start');
    Board := GetBoardSafe(0);
    ErrorMsg := BoardEditCheck(Board, ExpectedBoard);
    if ErrorMsg <> '' then
    begin
        Result := 'ERROR: ' + ErrorMsg;
        Exit;
    end;
    if (Trim(LayerName) = '') and (NetFilter.Count = 0) and (not HasWithin) and (not SelectedOnly) then
    begin
        Result := 'ERROR: Refusing to delete without a filter - give a layer, net_names, a within ' +
                  'rectangle or selected_only. Nothing was changed.';
        Exit;
    end;

    L := eNoLayer;
    if Trim(LayerName) <> '' then
    begin
        L := ResolveBoardLayer(Board, LayerName);
        if L = eNoLayer then
        begin
            Result := 'ERROR: Unknown layer "' + LayerName + '". Nothing was changed.';
            Exit;
        end;
    end;

    XO := Board.XOrigin;
    YO := Board.YOrigin;
    AX1 := XO + MilsToCoord(Min(WX1, WX2));
    AX2 := XO + MilsToCoord(Max(WX1, WX2));
    AY1 := YO + MilsToCoord(Min(WY1, WY2));
    AY2 := YO + MilsToCoord(Max(WY1, WY2));

    Candidates := CreateObject(TObjectList);
    Candidates.OwnsObjects := False;
    Report := TStringList.Create;
    Props := TStringList.Create;
    try
        Matched := 0;
        LoggedPoly := False;
        LoggedDim := False;
        for i := 0 to TypesList.Count - 1 do
        begin
            TypeName := LowerCase(Trim(TypesList[i]));
            BoardEditLog('collecting ' + TypeName + ' objects');
            Iterator := Board.BoardIterator_Create;
            if TypeName = 'track' then Iterator.AddFilter_ObjectSet(MkSet(eTrackObject))
            else if TypeName = 'arc' then Iterator.AddFilter_ObjectSet(MkSet(eArcObject))
            else if TypeName = 'via' then Iterator.AddFilter_ObjectSet(MkSet(eViaObject))
            else if TypeName = 'fill' then Iterator.AddFilter_ObjectSet(MkSet(eFillObject))
            else if TypeName = 'region' then Iterator.AddFilter_ObjectSet(MkSet(eRegionObject))
            else if TypeName = 'pad' then Iterator.AddFilter_ObjectSet(MkSet(ePadObject))
            else if TypeName = 'text' then Iterator.AddFilter_ObjectSet(MkSet(eTextObject))
            else if TypeName = 'polygon' then Iterator.AddFilter_ObjectSet(MkSet(ePolyObject))
            else
            begin
                Board.BoardIterator_Destroy(Iterator);
                Result := 'ERROR: Unknown object type "' + TypeName + '". Nothing was changed.';
                Exit;
            end;
            Iterator.AddFilter_LayerSet(AllLayers);
            Iterator.AddFilter_Method(eProcessAll);

            CanPour := (TypeName = 'track') or (TypeName = 'arc') or (TypeName = 'fill') or (TypeName = 'region');
            CanDim := (TypeName = 'track') or (TypeName = 'arc') or (TypeName = 'text');

            Prim := Iterator.FirstPCBObject;
            while (Prim <> nil) do
            begin
                // Free text carries no net, and asking a text object for one
                // is not something this codebase has proven safe.
                PrimNet := '';
                Keep := not Prim.InComponent;
                if Keep and CanPour then
                begin
                    if not LoggedPoly then
                    begin
                        BoardEditLog('checking Prim.InPolygon');
                        LoggedPoly := True;
                    end;
                    if Prim.InPolygon then Keep := False;
                end;
                if Keep and CanDim then
                begin
                    if not LoggedDim then
                    begin
                        BoardEditLog('checking Prim.InDimension');
                        LoggedDim := True;
                    end;
                    if Prim.InDimension then Keep := False;
                end;
                if Keep and (L <> eNoLayer) then
                begin
                    if TypeName = 'via' then
                        Keep := (Prim.LowLayer = L) or (Prim.HighLayer = L)
                    else
                        Keep := (Prim.Layer = L);
                end;
                if Keep and (TypeName <> 'text') then
                    PrimNet := PrimNetName(Prim);
                if Keep and (NetFilter.Count > 0) then
                    Keep := (NetFilter.IndexOf(PrimNet) >= 0);
                if Keep and HasWithin then
                    Keep := (Prim.BoundingRectangle.Left >= AX1) and (Prim.BoundingRectangle.Right <= AX2) and
                            (Prim.BoundingRectangle.Bottom >= AY1) and (Prim.BoundingRectangle.Top <= AY2);
                if Keep and SelectedOnly then
                    Keep := Prim.Selected;

                if Keep then
                begin
                    Inc(Matched);
                    if Candidates.Count < MaxObjects then
                        Candidates.Add(Prim);
                    if Report.Count < 200 then
                    begin
                        EProps := TStringList.Create;
                        try
                            AddJSONProperty(EProps, 'type', TypeName);
                            AddJSONProperty(EProps, 'layer', Board.LayerName(Prim.Layer));
                            AddJSONProperty(EProps, 'net', PrimNet);
                            AddJSONNumber(EProps, 'left', CoordToMils(Prim.BoundingRectangle.Left - XO));
                            AddJSONNumber(EProps, 'bottom', CoordToMils(Prim.BoundingRectangle.Bottom - YO));
                            AddJSONNumber(EProps, 'right', CoordToMils(Prim.BoundingRectangle.Right - XO));
                            AddJSONNumber(EProps, 'top', CoordToMils(Prim.BoundingRectangle.Top - YO));
                            Report.Add(BuildJSONObject(EProps));
                        finally
                            EProps.Free;
                        end;
                    end;
                end;
                Prim := Iterator.NextPCBObject;
            end;
            Board.BoardIterator_Destroy(Iterator);
        end;

        Deleted := 0;
        if (not DryRun) and (Matched > 0) and (Matched <= MaxObjects) then
        begin
            BoardEditLog('removing ' + IntToStr(Candidates.Count) + ' objects');
            PCBServer.PreProcess;
            for i := 0 to Candidates.Count - 1 do
            begin
                Prim := Candidates[i];
                Board.RemovePCBObject(Prim);
                Inc(Deleted);
            end;
            PCBServer.PostProcess;
            Board.ViewManager_FullUpdate;
            Client.SendMessage('PCB:Zoom', 'Action=Redraw', 255, Client.CurrentView);
        end;

        AddJSONProperty(Props, 'board_file', ExtractFileName(Board.FileName));
        AddJSONBoolean(Props, 'dry_run', DryRun);
        AddJSONInteger(Props, 'matched', Matched);
        AddJSONInteger(Props, 'deleted', Deleted);
        AddJSONInteger(Props, 'max_objects', MaxObjects);
        AddJSONBoolean(Props, 'over_limit', Matched > MaxObjects);
        AddJSONBoolean(Props, 'listing_truncated', Matched > Report.Count);
        Props.Add(Trim(BuildJSONArray(Report, 'objects')));
        BoardEditLog('delete_pcb_primitives: done');

        OutputLines := TStringList.Create;
        try
            OutputLines.Text := BuildJSONObject(Props);
            Result := WriteJSONToFile(OutputLines, ROOT_DIR + 'temp_delete_primitives.json');
        finally
            OutputLines.Free;
        end;
    finally
        Props.Free;
        Report.Free;
        Candidates.Free;
    end;
end;

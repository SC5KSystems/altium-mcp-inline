// READ-ONLY. UNVALIDATED: written without a licensed Altium to run it against.
//
// Dumps the board outline through the API so it can be compared with the offline
// reader (python -m offline info <board.PcbDoc>): same vertex count, same vertex
// coordinates, same arc centres and radius. Member names Kind / vx / vy are the ones
// get_polygon_data already uses; cx / cy / Radius / Angle1 / Angle2 are NOT proven here,
// so each is read in its own logged step - if the script stops, the last log line names
// the member that does not exist.
//
// EDIT: the board's full path as Altium has it open (File > Open it first)
S1 := 'C:\scratch\board.PcbDoc';
//
// Run:  python dev/sandbox_runner.py exp_board_outline_dump.pas
ResultText := '{"error": "the board in S1 is not open in Altium"}';
if (PCBServer <> nil) then
begin
    Obj1 := PCBServer.GetPCBBoardByPath(S1);
    if (Obj1 <> nil) then
    begin
        S3 := Obj1.FileName;
        S2 := '';
        for I3 := 1 to Length(S3) do
            if (S3[I3] = '\') then S2 := S2 + '/' else S2 := S2 + S3[I3];
        SandboxLog('board: ' + S2);
        Obj2 := Obj1.BoardOutline;
        SandboxLog('outline object fetched');
        I1 := Obj2.PointCount;
        SandboxLog('point count ' + IntToStr(I1));
        List1 := TStringList.Create;
        for I2 := 0 to I1 - 1 do
        begin
            SandboxLog('segment ' + IntToStr(I2) + ': kind and vertex');
            S1 := IntToStr(Obj2.Segments[I2].Kind) + ',' +
                  FloatToStr(CoordToMils(Obj2.Segments[I2].vx - Obj1.XOrigin)) + ',' +
                  FloatToStr(CoordToMils(Obj2.Segments[I2].vy - Obj1.YOrigin));
            if (Obj2.Segments[I2].Kind = 1) then
            begin
                SandboxLog('segment ' + IntToStr(I2) + ': arc centre');
                S1 := S1 + ',' + FloatToStr(CoordToMils(Obj2.Segments[I2].cx - Obj1.XOrigin)) + ',' +
                      FloatToStr(CoordToMils(Obj2.Segments[I2].cy - Obj1.YOrigin));
                SandboxLog('segment ' + IntToStr(I2) + ': arc radius');
                S1 := S1 + ',' + FloatToStr(CoordToMils(Obj2.Segments[I2].Radius));
                SandboxLog('segment ' + IntToStr(I2) + ': arc angles');
                S1 := S1 + ',' + FloatToStr(Obj2.Segments[I2].Angle1) + ',' + FloatToStr(Obj2.Segments[I2].Angle2);
            end;
            List1.Add(S1);
        end;
        S4 := '';
        for I2 := 0 to List1.Count - 1 do
        begin
            if (I2 > 0) then S4 := S4 + ', ';
            S4 := S4 + '"' + List1.Strings[I2] + '"';
        end;
        // units: mils, relative to the board origin. Format: kind,x,y[,cx,cy,radius,angle1,angle2]
        ResultText := '{"file": "' + S2 + '", "units": "mil", "segments": [' + S4 + ']}';
        SandboxLog('done');
    end;
end;

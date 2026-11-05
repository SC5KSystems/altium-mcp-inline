// MOVES A PRIMITIVE unless B1 = 1 (dry run, the default). UNVALIDATED: written without a
// licensed Altium to run it against. Use on a SCRATCH COPY of the board only.
//
// Moves one mounting hole: a pad that belongs to no component. Finds it by its current
// position (mils, relative to the board origin); refuses unless exactly one free pad is
// within 5 mil, so a wrong coordinate moves nothing. Refuses boards on the N: drive.
//
// EDIT THESE FOUR LINES (the defaults are placeholders: a hole at 1000, 1000 mil moving 30 mil
// in x and y) AND B1:
I1 := 1000;    // current x, mils
I2 := 1000;    // current y, mils
I3 := 1030;    // new x, mils
I4 := 1030;    // new y, mils
B1 := 1;       // 1 = report only; set 0 to move
//
// EDIT: the board's full path as Altium has it open (File > Open it first)
S1 := 'C:\scratch\board.PcbDoc';
//
// Run:  python dev/sandbox_runner.py exp_move_free_pad.pas
ResultText := '{"error": "the board in S1 is not open in Altium"}';
if (PCBServer <> nil) then
begin
    Obj1 := PCBServer.GetPCBBoardByPath(S1);
    if (Obj1 <> nil) then
    begin
        S3 := Obj1.FileName;
        S2 := '';
        for I5 := 1 to Length(S3) do
            if (S3[I5] = '\') then S2 := S2 + '/' else S2 := S2 + S3[I5];
        SandboxLog('board: ' + S2);
        if (Uppercase(Copy(S3, 1, 2)) = 'N:') then
        begin
            ResultText := '{"error": "board is on the N: drive; refusing to edit it"}';
            SandboxLog('ABORT: N: drive');
        end
        else
        begin
            SandboxLog('scanning pads');
            S1 := '';
            B1MAX := 0;          // count of free pads within tolerance
            Obj3 := nil;
            Obj2 := Obj1.BoardIterator_Create;
            Obj2.AddFilter_ObjectSet(MkSet(ePadObject));
            Obj2.AddFilter_LayerSet(AllLayers);
            Obj2.AddFilter_Method(eProcessAll);
            Obj4 := Obj2.FirstPCBObject;
            while (Obj4 <> nil) do
            begin
                if (not Obj4.InComponent) then
                    if (Abs(CoordToMils(Obj4.x - Obj1.XOrigin) - I1) < 5) and
                       (Abs(CoordToMils(Obj4.y - Obj1.YOrigin) - I2) < 5) then
                    begin
                        B1MAX := B1MAX + 1;
                        Obj3 := Obj4;
                        S1 := S1 + IntToStr(Round(CoordToMils(Obj4.x - Obj1.XOrigin))) + ',' +
                              IntToStr(Round(CoordToMils(Obj4.y - Obj1.YOrigin))) + ' hole ' +
                              FloatToStr(CoordToMils(Obj4.HoleSize)) + ' mil; ';
                    end;
                Obj4 := Obj2.NextPCBObject;
            end;
            Obj1.BoardIterator_Destroy(Obj2);
            SandboxLog('free pads within tolerance: ' + IntToStr(B1MAX));

            if (B1MAX <> 1) then
                ResultText := '{"error": "expected exactly one free pad at that position", "found": ' + IntToStr(B1MAX) + '}'
            else if (B1 = 1) then
                ResultText := '{"dry_run": true, "would_move": "' + S1 + '"}'
            else
            begin
                SandboxLog('moving');
                PCBServer.PreProcess;
                PCBServer.SendMessageToRobots(Obj3.I_ObjectAddress, c_Broadcast, PCBM_BeginModify, c_NoEventData);
                Obj3.MoveToXY(MilsToCoord(I3) + Obj1.XOrigin, MilsToCoord(I4) + Obj1.YOrigin);
                PCBServer.SendMessageToRobots(Obj3.I_ObjectAddress, c_Broadcast, PCBM_EndModify, c_NoEventData);
                PCBServer.PostProcess;
                Client.SendMessage('PCB:Zoom', 'Action=Redraw', 255, Client.CurrentView);
                ResultText := '{"moved": true, "from": "' + S1 + '", "note": "board is modified and unsaved"}';
                SandboxLog('moved');
            end;
        end;
    end;
end;
